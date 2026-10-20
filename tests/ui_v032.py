"""Synthetic dashboard check.

Serves the extension over localhost and mocks Chrome APIs inside Chromium.
No Native Messaging, real profile, real log or account data is used.
"""
from copy import deepcopy
from datetime import date, datetime, time as clock_time, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
SCREENSHOTS = ROOT / "artifacts" / "screenshots"

TODAY = date.today()
DAYS = [(TODAY - timedelta(days=offset)).isoformat() for offset in range(3)]
STAMP = int(datetime.now().timestamp())


def day_epoch(offset, hour=12):
    return int(datetime.combine(TODAY + timedelta(days=offset), clock_time(hour)).timestamp())

SESSION = {
    "outcome": "resumed", "actualSessionId": "cli-session", "requested": "cli-session",
    "requestedValues": ["cli-session"], "runCount": 3, "confirmedResumeCount": 2,
    "newSessionCount": 1, "mismatchCount": 0, "failedCount": 0,
    "unconfirmedCount": 0, "unknownCount": 0, "contextCompression": None,
    "runsTruncated": False,
    "runs": [
        {"runId": "run-new", "requested": None, "actual": "cli-session",
         "status": "completed", "completed": True, "outcome": "new"},
        {"runId": "run-resume", "requested": "cli-session", "actual": "cli-session",
         "status": "completed", "completed": True, "outcome": "resumed"},
        {"runId": "run-resume-2", "requested": "cli-session", "actual": "cli-session",
         "status": "completed", "completed": True, "outcome": "resumed"},
    ],
}

GROUPS = [
    {"day": DAYS[0], "source": "codex", "model": "gpt-6.1-sol", "mode": "standard",
     "records": 1, "timed": 1, "input": 1000, "output": 100, "cacheHit": .75,
     "cacheKnown": 1, "cacheMissing": 0, "cost": .01, "priced": 1, "credit": None,
     "creditCount": 0, "creditApplicable": 0, "average": 10, "median": 10,
     "minimum": 10, "maximum": 10, "responseTurns": 1, "responseSamples": 1,
     "responseMedian": 1.2, "responseP90": 1.2, "frozen": False},
    {"day": DAYS[0], "source": "workbuddy-desktop", "model": "deepseek-v4.1-flash",
     "mode": "", "records": 1, "timed": 0, "input": 500, "output": 50, "cacheHit": None,
     "cacheKnown": 0, "cacheMissing": 1, "cost": None, "priced": 0, "credit": None,
     "creditCount": 0, "creditApplicable": 1, "average": None, "median": None,
     "minimum": None, "maximum": None, "responseTurns": 2, "responseSamples": 2,
     "responseMedian": 2.4, "responseP90": 2.8, "frozen": False},
    {"day": DAYS[0], "source": "codebuddy-cli", "model": "deepseek-v4.1-flash",
     "mode": "", "records": 1, "timed": 1, "input": 2000, "output": 200, "cacheHit": .5,
     "cacheKnown": 1, "cacheMissing": 0, "cost": .01, "priced": 1, "credit": .06,
     "creditCount": 1, "creditApplicable": 1, "average": 40, "median": 40,
     "minimum": 40, "maximum": 40, "responseTurns": 0, "responseSamples": 0,
     "responseMedian": None, "responseP90": None, "frozen": False},
    {"day": DAYS[1], "source": "codex", "model": "gpt-6.1-sol", "mode": "standard",
     "records": 1, "timed": 1, "input": 800, "output": 80, "cacheHit": .7,
     "cacheKnown": 1, "cacheMissing": 0, "cost": .008, "priced": 1, "credit": None,
     "creditCount": 0, "creditApplicable": 0, "average": 8, "median": 8,
     "minimum": 8, "maximum": 8, "responseTurns": 1, "responseSamples": 1,
     "responseMedian": 1.4, "responseP90": 1.4, "frozen": False},
    {"day": DAYS[1], "source": "workbuddy-desktop", "model": "deepseek-v4.1-flash",
     "mode": "", "records": 1, "timed": 0, "input": 400, "output": 40, "cacheHit": None,
     "cacheKnown": 0, "cacheMissing": 1, "cost": None, "priced": 0, "credit": None,
     "creditCount": 0, "creditApplicable": 1, "average": None, "median": None,
     "minimum": None, "maximum": None, "responseTurns": 1, "responseSamples": 1,
     "responseMedian": 2.8, "responseP90": 2.8, "frozen": False},
    {"day": DAYS[2], "source": "codebuddy-cli", "model": "deepseek-v4.1-flash",
     "mode": "", "records": 1, "timed": 1, "input": 200, "output": 20, "cacheHit": .6,
     "cacheKnown": 1, "cacheMissing": 0, "cost": 0, "priced": 1, "credit": .02,
     "creditCount": 1, "creditApplicable": 1, "average": 20, "median": 20,
     "minimum": 20, "maximum": 20, "responseTurns": 1, "responseSamples": 1,
     "responseMedian": .9, "responseP90": .9, "frozen": False},
    {"day": DAYS[2], "source": "codex", "model": "gpt-6.1-sol", "mode": "standard",
     "records": 1, "timed": 1, "input": 100, "output": 10, "cacheHit": .8,
     "cacheKnown": 1, "cacheMissing": 0, "cost": .002, "priced": 1, "credit": None,
     "creditCount": 0, "creditApplicable": 0, "average": 10, "median": 10,
     "minimum": 10, "maximum": 10, "responseTurns": 1, "responseSamples": 1,
     "responseMedian": 1.1, "responseP90": 1.1, "frozen": False},
]


def detail_row(key, index, group, at, **extra):
    row = {
        "key": key, "at": at, "model": group["model"], "mode": group["mode"],
        "input": group["input"], "output": group["output"], "cacheHit": group["cacheHit"],
        "cacheKnown": bool(group["cacheKnown"]), "cost": group["cost"],
        "credit": group["credit"], "creditValid": group["credit"] is not None,
        "speed": group["average"], "duration": 10 if group["average"] else None,
        "firstToken": None, "turnId": f"turn-{index}", "firstInTurn": False,
        "sessionId": f"session-{group['source']}", "source": f"projects/{group['source']}.jsonl",
        "sourceId": group["source"], "messageId": key, "requestId": key, "session": None,
        "modeSource": "log", "modeAssumed": False, "longContext": False,
        "pricingVersion": "2026-10-09-v1", "timingSource": "boundary",
        "pricingBasis": "合成价格快照",
    }
    row.update(extra)
    return row


DETAILS = [
    detail_row("detail-codex-today", 1, GROUPS[0], STAMP - 30, firstToken=1.2, firstInTurn=True),
    detail_row("detail-buddy-today", 2, GROUPS[1], STAMP - 20, firstToken=2.4,
               firstTokenSource="request", timingSource=None, speed=None, duration=None),
    detail_row("detail-cli-today", 3, GROUPS[2], STAMP - 10, firstToken=None,
               session=SESSION, timingSource="boundary", speed=40, duration=5),
    detail_row("detail-codex-yesterday", 4, GROUPS[3], STAMP - 86400, firstToken=1.4,
               firstInTurn=True),
    detail_row("detail-buddy-yesterday", 5, GROUPS[4], STAMP - 86430, firstToken=2.8,
               firstTokenSource="request", timingSource=None, speed=None, duration=None),
    detail_row("detail-cli-2days", 6, GROUPS[5], STAMP - 172800, firstToken=.9,
               session=SESSION, speed=20, duration=1),
    detail_row("detail-codex-2days", 7, GROUPS[6], STAMP - 172830, firstToken=1.1,
               firstInTurn=True),
]


def cards(rows):
    records = len(rows)
    timed_value = lambda row: row["speed"] if "speed" in row else row.get("average")
    known = [row for row in rows if row.get("cacheKnown")]
    cache_hit = (sum(row["cacheHit"] * row["input"] for row in known) /
                 sum(row["input"] for row in known)
                 if known and len(known) == records else None)
    credits = [row["credit"] for row in rows
               if row.get("creditValid") or row.get("creditCount")]
    codex_records = sum((row.get("sourceId") or row.get("source")) == "codex" for row in rows)
    costs = [row["cost"] for row in rows if row.get("cost") is not None]
    return {
        "records": records, "codexRecords": codex_records,
        "creditApplicable": records - codex_records, "timed":
        sum(timed_value(row) is not None for row in rows),
        "input": sum(row["input"] for row in rows), "output": sum(row["output"] for row in rows),
        "cache": sum((row.get("cacheHit") or 0) * row["input"] for row in known),
        "cacheHit": cache_hit, "cacheKnown": len(known), "cost": sum(costs) if costs else None,
        "priced": len(costs), "average": 20 if records else None, "traceTimed": 0,
        "boundaryTimed": sum(timed_value(row) is not None for row in rows),
        "responseTurns": sum(row.get("responseTurns", 0) for row in rows),
        "responseSamples": sum(row.get("responseSamples", 0) for row in rows),
        "responseMedian": None, "responseP90": None,
        "credit": sum(credits) if credits else None, "creditCount": len(credits),
        "creditMissing": records - len(credits),
    }


QUOTA = {
    "source": "account", "fetchedAt": STAMP - 120,
    "buckets": [{"name": "Codex", "plan": "Plus", "windows": [
        {"kind": "primary", "minutes": 300, "used": 42, "remaining": 58,
         "resetAt": STAMP + 3600},
        {"kind": "secondary", "minutes": 10080, "used": 61, "remaining": 39,
         "resetAt": STAMP + 86400},
    ]}],
    "buddy": {
        "status": "known", "plan": "标准版", "remaining": 6450.5, "total": 8000,
        "used": 1549.5, "frozen": 0, "unit": "credit",
        "groups": [
            {"kind": "plan_base", "name": "套餐基础积分", "remaining": 4760.5,
             "total": 6050, "used": 1289.5, "frozen": 0},
            {"kind": "bonus", "name": "补充/活动积分", "remaining": 1690,
             "total": 1950, "used": 260, "frozen": 0},
        ],
        "packages": [
            {"name": "限时补充积分", "remaining": 1200, "total": 1200, "used": 0,
             "expireAt": day_epoch(1, 10), "refreshAt": None,
             "showExpireAt": True, "showRefreshAt": False},
            {"name": "活动赠送积分", "remaining": 340, "total": 500, "used": 160,
             "expireAt": None, "refreshAt": None,
             "showExpireAt": True, "showRefreshAt": False},
            {"name": "套餐基础积分", "remaining": 3000, "total": 4000, "used": 1000,
             "expireAt": day_epoch(1, 15), "refreshAt": day_epoch(4, 12),
             "showExpireAt": True, "showRefreshAt": True},
            {"name": "套餐基础积分", "remaining": 80, "total": 100, "used": 20,
             "expireAt": day_epoch(2, 12), "refreshAt": day_epoch(5, 12),
             "showExpireAt": True, "showRefreshAt": True},
            {"name": "周期积分", "remaining": 50, "total": 100, "used": 50,
             "expireAt": day_epoch(1, 10), "refreshAt": day_epoch(4, 12),
             "showExpireAt": False, "showRefreshAt": True},
            {"name": "远端积分", "remaining": 100, "total": 150, "used": 50,
             "expireAt": day_epoch(5, 12), "refreshAt": None,
             "showExpireAt": True, "showRefreshAt": False},
            {"name": "套餐基础积分", "remaining": 1680.5, "total": 1950, "used": 269.5,
             "expireAt": day_epoch(3, 12), "refreshAt": None,
             "showExpireAt": True, "showRefreshAt": False},
        ],
        "packagesStatus": "known", "packagesError": None,
        "fetchedAt": STAMP - 90, "lastAttempt": STAMP - 90,
        "source": {"kind": "account", "label": "WorkBuddy 账号"},
        "error": None,
    },
}

KNOWN_BUDDY = deepcopy(QUOTA["buddy"])

STATUS = {
    "home": "C:/Synthetic/Codex", "version": 12, "records": len(DETAILS),
    "earliest": STAMP - 180000, "latest": STAMP, "lastSuccess": STAMP,
    "lastAttempt": STAMP, "lastImport": {"added": 2, "historical": 0},
    "pricingVersion": "2026-10-09-v1", "pricingChecked": "2026-10-12",
    "models": ["gpt-6.1-sol"],
    "sourceModels": {"codex": ["gpt-6.1-sol"],
                     "workbuddy-desktop": ["deepseek-v4.1-flash"],
                     "codebuddy-cli": ["deepseek-v4.1-flash"]},
    "allModels": ["deepseek-v4.1-flash", "gpt-6.1-sol"],
    "sources": [
        {"id": "codex", "label": "Codex", "available": True, "records": 3},
        {"id": "workbuddy-desktop", "label": "WorkBuddy 桌面端", "available": True, "records": 2},
        {"id": "codebuddy-cli", "label": "CodeBuddy CLI", "available": True, "records": 2},
    ],
    "buddy": {
        "enabled": True,
        "roots": {"workbuddy": "C:/Synthetic/.workbuddy/projects",
                  "codebuddy": "C:/Synthetic/.codebuddy/projects",
                  "bridge": "C:/Synthetic/runs"},
        "sources": {
            "workbuddy-desktop": {"root": "C:/Synthetic/.workbuddy/projects", "exists": True,
                                  "files": 2, "records": 2, "latest": STAMP - 20},
            "codebuddy-cli": {"root": "C:/Synthetic/.codebuddy/projects", "exists": True,
                              "files": 2, "records": 2, "latest": STAMP - 10},
        },
        "bridge": {"root": "C:/Synthetic/runs", "exists": True, "runs": 3, "sessions": 1,
                   "resumed": 2, "new": 1, "mismatch": 0, "unconfirmed": 0, "failed": 0},
        "lastAttempt": STAMP, "lastSuccess": STAMP, "lastImport": {"added": 1},
        "models": {"workbuddy-desktop": ["deepseek-v4.1-flash"],
                   "codebuddy-cli": ["deepseek-v4.1-flash"]},
    },
    "appVersion": "0.3.3", "responseBackfillPending": False, "quota": QUOTA,
}

STATE = {"busy": False, "error": None, "status": STATUS, "nextAlarm": STAMP + 60,
         "progress": None, "quota": QUOTA, "quotaBusy": False, "refreshMinutes": 1}


def in_range(row, params):
    return params.get("from", "0000") <= row["day"] <= params.get("to", "9999")


def filtered_stats(params):
    rows = [row for row in GROUPS if in_range(row, params)]
    if params.get("source") not in (None, "", "all"):
        rows = [row for row in rows if row["source"] == params["source"]]
    if params.get("model"):
        rows = [row for row in rows if row["model"] == params["model"]]
    return rows


def filtered_details(params):
    rows = [row for row in DETAILS if params.get("from", "0000") <= date.fromtimestamp(row["at"]).isoformat() <= params.get("to", "9999")]
    source = params.get("source", "all")
    if source != "all":
        rows = [row for row in rows if row["sourceId"] == source]
    if params.get("model"):
        rows = [row for row in rows if row["model"] == params["model"]]
    return sorted(rows, key=lambda row: row["at"], reverse=True)


def mock_stats(params):
    rows = filtered_stats(params)
    page, chunk = int(params.get("page", 1)), 2
    part = rows[(page - 1) * chunk:page * chunk]
    return {"status": STATUS, "cards": cards(rows), "page": page, "pageSize": chunk,
            "source": "all", "total": len(rows), "rows": part,
            "nextPage": page + 1 if page * chunk < len(rows) else None}


def mock_details(params):
    rows = filtered_details(params)
    page, size = int(params.get("page", 1)), int(params.get("pageSize", 50))
    return {"status": STATUS, "cards": cards(rows), "page": page, "pageSize": size,
            "source": params.get("source", "all"), "total": len(rows),
            "rows": rows[(page - 1) * size:page * size]}


def screenshot(page, name):
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / name), full_page=True)


def bump_state():
    STATUS["version"] += 1
    return STATE


def run():
    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                          ".mjs": "text/javascript", ".css": "text/css"}

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(EXTENSION)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/dashboard.html"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            page = browser.new_page(viewport={"width": 1800, "height": 1150}, locale="zh-CN",
                                    timezone_id="Asia/Shanghai")
            page.expose_function("mockState", lambda: STATE)
            page.expose_function("mockDetails", mock_details)
            page.expose_function("mockStats", mock_stats)
            page.expose_function("mockBumpState", bump_state)
            page.expose_function("mockConfigure", lambda params: {
                "home": STATUS["home"], "buddy": STATUS["buddy"]["roots"]})
            page.add_init_script("""(() => {
                const store = {preferences: {tab:'details', preset:'3', source:'codex',
                    model:'gpt-6.1-sol', mode:'legacy', pageSize:50, sort:'time',
                    direction:'desc', statsDirection:'desc', metric:'average',
                    usageMetric:'output', currency:'USD'}};
                window.__mockStore = store;
                window.__mockCalls = {stats: 0, details: 0};
                const listeners = [];
                window.chrome = {
                    runtime: {
                        id: 'synthetic-032',
                        sendMessage: async (message) => {
                            const op = message.op;
                            if (op === 'state' || op === 'visit') return {ok:true, data: await window.mockState()};
                            if (op === 'details') { window.__mockCalls.details += 1; return {ok:true, data: await window.mockDetails(message.params)}; }
                            if (op === 'stats') { window.__mockCalls.stats += 1; return {ok:true, data: await window.mockStats(message.params)}; }
                            if (op === 'configure') return {ok:true, data: await window.mockConfigure(message.params)};
                            if (op === 'quota') return {ok:true, data:{}};
                            if (op === 'refresh') return {ok:true, data:{import:{partial:false}, status:(await window.mockState()).status}};
                            return {ok:false, error:'unsupported'};
                        },
                    },
                    storage: { local: {
                        get: async (keys) => Array.isArray(keys)
                            ? Object.fromEntries(keys.map((key) => [key, store[key]]))
                            : typeof keys === 'string' ? {[keys]: store[keys]} : {...store},
                        set: async (values) => Object.assign(store, values),
                    }, onChanged: { addListener: (listener) => listeners.push(listener) } },
                };
                window.__emitMonitorState = (value) => {
                    for (const listener of listeners) listener({monitorState: {newValue: value}}, 'local');
                };
            })();""")
            page.goto(url)
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            expect(page.locator("#usage-chart svg")).to_be_visible(timeout=15000)

            # Common region and filter removal.
            assert page.locator("#source").count() == 0
            assert page.locator("#model").count() == 0
            expect(page.locator(".common-scope")).to_have_text("全部工具与模型")
            expect(page.locator("#preset")).to_be_visible()
            expect(page.locator("#card-cost")).to_have_text("~$0.0300")
            expect(page.locator("#card-priced")).to_contain_text("已计价 5/7 条")
            expect(page.locator("#card-timed")).to_have_text("5 条可测速 / 7 条记录")
            footnote = page.locator(".footnote").inner_text()
            assert "后端恢复后" not in footnote
            assert "按模型请求记录" in footnote and "可能包含思考或工具片段" in footnote
            assert "诊断 TTFT" not in footnote
            assert "基于结束轮次" not in footnote
            assert page.evaluate("window.__mockStore.preferences.source") is None
            assert page.evaluate("window.__mockStore.preferences.model") is None
            assert page.evaluate("getComputedStyle(document.querySelector('.common-controls')).position") == "static"
            assert page.evaluate("getComputedStyle(document.querySelector('#chart-panel')).position") == "static"
            toolbar = page.locator(".table-toolbar").bounding_box()
            heading = page.locator(".table-heading").bounding_box()
            table = page.locator("#table-wrap").bounding_box()
            assert toolbar["y"] + toolbar["height"] <= heading["y"] + 1
            assert heading["y"] + heading["height"] <= table["y"] + 1
            assert page.locator(".table-toolbar [data-tab='details']").count() == 1
            sources = page.locator("tbody .source-cell").all_text_contents()
            assert "Codex" in sources and "WorkBuddy 桌面端" in sources and "CodeBuddy CLI" in sources
            screenshot(page, "v032-common-details.png")

            # Stacked output bars combine the same model across tools.
            today_group = page.locator(f"#usage-chart .usage-day[data-day='{DAYS[0]}']")
            expect(today_group).to_have_attribute("data-total", "350")
            segments = page.locator(f"#usage-chart .usage-day[data-day='{DAYS[0]}'] .usage-segment")
            values = {(segment.get_attribute("data-model"), segment.get_attribute("data-value"))
                      for segment in segments.all()}
            assert ("deepseek-v4.1-flash", "250") in values
            assert ("gpt-6.1-sol", "100") in values
            chart_box = page.locator("#usage-chart").bounding_box()
            for segment in segments.all():
                box = segment.bounding_box()
                assert box["x"] >= chart_box["x"] - 1
                assert box["x"] + box["width"] <= chart_box["x"] + chart_box["width"] + 1
            last = page.locator(f"#usage-chart .usage-day[data-day='{DAYS[2]}'] .usage-segment").last
            last_box = last.bounding_box()
            assert last_box["x"] + last_box["width"] <= chart_box["x"] + chart_box["width"] + 1

            # Narrow layout keeps the first/last stacked bars inside the plot.
            page.set_viewport_size({"width": 900, "height": 1100})
            page.wait_for_function("""() => {
                const chart = document.querySelector('#usage-chart');
                const svg = chart && chart.querySelector('svg');
                return svg && Number(svg.getAttribute('viewBox').split(' ')[2]) <= Math.max(280, chart.clientWidth);
            }""")
            bars = page.evaluate("""() => {
                const svg = document.querySelector('#usage-chart svg');
                const width = Number(svg.getAttribute('viewBox').split(' ')[2]);
                return [...svg.querySelectorAll('.usage-day')].map((group) => ({
                    day: group.getAttribute('data-day'),
                    bars: [...group.querySelectorAll('.usage-segment')].map((rect) => ({
                        x: Number(rect.getAttribute('x')),
                        w: Number(rect.getAttribute('width')),
                        width,
                    })),
                }));
            }""")
            assert len(bars) == 3
            for group in bars:
                for rect in group["bars"]:
                    assert rect["x"] >= 0
                    assert rect["x"] + rect["w"] <= rect["width"]
            screenshot(page, "v032-narrow.png")
            page.set_viewport_size({"width": 1800, "height": 1150})
            page.wait_for_function("""() => {
                const chart = document.querySelector('#usage-chart');
                const svg = chart && chart.querySelector('svg');
                return svg && Number(svg.getAttribute('viewBox').split(' ')[2]) > 700;
            }""")

            # Hover and keyboard focus both expose the same model breakdown.
            page.mouse.move(0, 0)
            today_group.hover()
            tooltip = page.locator("#usage-chart .usage-tooltip")
            expect(tooltip).to_be_visible()
            text = tooltip.inner_text()
            assert DAYS[0] in text and "合计 350 token" in text
            assert "gpt-6.1-sol" in text and "deepseek-v4.1-flash" in text
            assert "100 token" in text and "250 token" in text and "3 条请求记录" in text
            page.mouse.move(0, 0)
            expect(tooltip).to_be_hidden()
            today_group.focus()
            expect(tooltip).to_be_visible()
            assert DAYS[0] in tooltip.inner_text()

            # Cost keeps partial/unknown counts distinct from known zero.
            page.locator("#usage-metric").select_option("cost")
            expect(page.locator("#usage-summary")).to_contain_text("2 条未计价")
            expect(page.locator("#usage-summary")).to_contain_text("~$0.0300")
            expect(page.locator("#usage-legend")).to_contain_text("含未计价记录")
            expect(page.locator(f"#usage-chart .usage-day[data-day='{DAYS[0]}'] .usage-unknown-cap")).to_have_count(1)
            today_group.hover()
            expect(tooltip).to_be_visible()
            SCREENSHOTS.mkdir(parents=True, exist_ok=True)
            page.locator("#usage-chart").screenshot(path=str(SCREENSHOTS / "v032-cost-tooltip.png"))
            today_group.focus()
            expect(tooltip).to_be_visible()
            cost_text = tooltip.inner_text()
            assert "已知合计 ~$0.0200" in cost_text and "1 条未计价" in cost_text
            assert "~$0.0100 · 1/2 条已计价 · 1 条未知" in cost_text
            assert "不按 $0 或免费计算" in cost_text
            page.mouse.move(0, 0)
            day2 = page.locator(f"#usage-chart .usage-day[data-day='{DAYS[2]}']")
            day2.focus()
            expect(tooltip).to_be_visible()
            assert "已知 0" in tooltip.inner_text()
            page.mouse.move(0, 0)
            screenshot(page, "v032-cost-unknown.png")

            # Currency change uses the fixed 6.8 rate in cards, table and tooltip.
            page.locator("#settings").click()
            expect(page.locator("#settings-dialog")).to_be_visible()
            page.locator("#currency").select_option("CNY")
            page.locator("#save-home").click()
            expect(page.locator("#settings-dialog")).to_be_hidden()
            expect(page.locator("#card-cost")).to_have_text("~¥0.2040")
            expect(page.locator("#usage-summary")).to_contain_text("~¥0.2040")
            today_group.focus()
            assert "~¥0.1360" in tooltip.inner_text()
            assert "~¥0.0680" in tooltip.inner_text()
            page.mouse.move(0, 0)

            # Daily table retains its twelve headers; common region does not move.
            page.locator("[data-tab='stats']").click()
            expect(page.locator("#loading")).to_be_hidden()
            headers = [text.replace("↓", "").replace("↑", "")
                       for text in page.locator("thead th").all_text_contents()]
            assert headers == ["日期", "工具", "模型", "样本", "输入", "输出", "缓存命中",
                               "成本", "首字", "平均", "最低", "最高"]
            expect(page.locator("#card-records")).to_have_text("7")
            expect(page.locator("#preset")).to_be_visible()
            expect(page.locator("#chart svg")).to_be_visible()
            expect(page.locator("#usage-chart svg")).to_be_visible()
            workbuddy_daily = page.locator("tbody tr").filter(has_text="WorkBuddy 桌面端").filter(has_text=DAYS[0]).first
            expect(workbuddy_daily).to_contain_text("2/2 次请求")
            codex_daily = page.locator("tbody tr").filter(has_text="Codex").filter(has_text=DAYS[0]).first
            expect(codex_daily).to_contain_text("1/1 轮")
            page.locator("#metric").select_option("responseMedian")
            expect(page.locator("#trend-note")).to_contain_text("个有效样本")
            page.locator("#metric").select_option("average")
            summary_before = page.locator("#usage-summary").inner_text()
            screenshot(page, "v032-common-stats.png")

            # Silent stats refresh preserves window and table scroll with real overflow.
            page.set_viewport_size({"width": 1800, "height": 900})
            page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            page.evaluate("window.scrollTo(0, 320)")
            page.locator("#table-wrap").evaluate("(element) => { element.scrollTop = 90; }")
            saved_y = page.evaluate("window.scrollY")
            saved_top = page.locator("#table-wrap").evaluate("(element) => element.scrollTop")
            assert saved_y > 0 and saved_top > 0
            before_stats = page.evaluate("window.__mockCalls.stats")
            page.evaluate("async () => window.__emitMonitorState(await window.mockBumpState())")
            page.wait_for_function(f"window.__mockCalls.stats >= {before_stats + 4}")
            page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            assert abs(page.evaluate("window.scrollY") - saved_y) <= 1
            assert abs(page.locator("#table-wrap").evaluate("(element) => element.scrollTop") - saved_top) <= 1
            page.set_viewport_size({"width": 1800, "height": 1150})
            page.evaluate("window.scrollTo(0, 0)")

            # Daily row click scopes only the table and is visibly resettable.
            row = page.locator("tbody tr").filter(has_text="WorkBuddy 桌面端").filter(has_text=DAYS[0]).first
            row.click()
            expect(page.locator("[data-tab='details']")).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#table-scope")).to_be_visible()
            expect(page.locator("#table-scope-label")).to_contain_text(DAYS[0])
            expect(page.locator("#table-scope-label")).to_contain_text("WorkBuddy 桌面端")
            expect(page.locator("#preset")).to_have_value("3")
            expect(page.locator("#card-records")).to_have_text("7")
            expect(page.locator("#usage-summary")).to_have_text(summary_before)
            expect(page.locator("tbody tr")).to_have_count(1)
            expect(page.locator("tbody .source-cell")).to_have_text("WorkBuddy 桌面端")
            page.locator("#clear-scope").click()
            expect(page.locator("#table-scope")).to_be_hidden()
            expect(page.locator("tbody tr")).to_have_count(7)

            # Buddy first token is data-driven, with a source-specific explanation.
            buddy_row = page.locator("tbody tr").filter(has_text="WorkBuddy 桌面端").first
            buddy_row.locator(".view-record").click()
            expect(page.locator("#detail-dialog")).to_be_visible()
            detail_text = page.locator("#detail-fields").inner_text()
            assert "2.400 秒" in detail_text
            assert "WorkBuddy/CLI 按每次模型请求记录首字" in detail_text
            assert "首字来源" in detail_text and "按请求记录" in detail_text
            assert "可能包含思考或工具片段" in page.locator("#detail-dialog .dialog-note").inner_text()
            page.locator('[data-close="detail-dialog"]').click()
            codex_row = page.locator("tbody tr").filter(has_text="Codex").first
            codex_row.locator(".view-record").click()
            detail_text = page.locator("#detail-fields").inner_text()
            assert "1.200 秒" in detail_text
            assert "Codex 每轮仅首个请求记录一次" in detail_text
            page.locator('[data-close="detail-dialog"]').click()

            # Split quota: stacked Codex bars left, WorkBuddy expiry ring right.
            expect(page.locator(".quota-heading h2")).to_have_text("账号额度")
            expect(page.locator(".quota-heading p")).to_contain_text("不受日期筛选影响")
            expect(page.locator(".quota-split")).to_have_count(1)
            expect(page.locator(".quota-codex .codex-window-row")).to_have_count(2)
            split_boxes = page.locator(".quota-split > section").evaluate_all(
                "(elements) => elements.map((element) => element.getBoundingClientRect())")
            assert split_boxes[0]["x"] + split_boxes[0]["width"] <= split_boxes[1]["x"] + 1
            panel_height = page.locator(".quota-panel").bounding_box()["height"]
            assert panel_height <= 250
            codex_text = page.locator(".quota-codex").inner_text()
            assert "5 小时额度" in codex_text and "每周额度" in codex_text
            assert "58%" in codex_text and "42%" in codex_text
            assert "39%" in codex_text and "61%" in codex_text
            assert "重置" in codex_text and "PLUS" in codex_text
            assert "账号接口" in page.locator(".quota-codex .quota-side-meta").inner_text()
            buddy_panel = page.locator(".quota-buddy")
            buddy_text = buddy_panel.inner_text()
            assert "标准版" in buddy_text and "同账号与 CodeBuddy 共用" in buddy_text
            assert "已同步" in buddy_text and "6,450.5" in buddy_text and "credit" in buddy_text
            assert "总量 8,000" in buddy_text and "已用 1,549.5" in buddy_text
            assert "查看积分包" in buddy_text and "WorkBuddy 账号" in buddy_text and "更新" in buddy_text
            assert "冻结" not in buddy_text and "账号接口" not in buddy_text
            assert "5 小时额度" not in buddy_text and "每周额度" not in buddy_text
            expect(page.locator(".quota-buddy .buddy-package")).to_have_count(0)
            SCREENSHOTS.mkdir(parents=True, exist_ok=True)
            page.locator(".quota-panel").screenshot(path=str(SCREENSHOTS / "v032-quota-ring.png"))

            # Expiry-day ring: readable groups, true sum, refresh-only excluded.
            segments = page.locator(".quota-buddy .ring-segment")
            expect(segments).to_have_count(6)
            labels = segments.evaluate_all("(elements) => elements.map((element) => element.dataset.label)")
            assert "较晚到期" in labels and "周期刷新" in labels and "时间未知" in labels
            assert sum(1 for label in labels if label not in ("较晚到期", "周期刷新", "时间未知")) == 3
            values = segments.evaluate_all("(elements) => elements.map((element) => Number(element.dataset.value))")
            assert abs(sum(values) - 6450.5) <= 0.001
            assert "周期刷新" in page.locator(".quota-buddy .ring-legend").inner_text()

            ring_tooltip = page.locator(".quota-buddy .ring-tooltip")
            day_segment = page.locator(".quota-buddy .ring-segment[data-key^='day:']").first
            ring_box = page.locator(".quota-buddy .ring-wrap").bounding_box()
            page.mouse.move(ring_box["x"] + ring_box["width"] * (56 + 43) / 112,
                            ring_box["y"] + ring_box["height"] * 56 / 112)
            expect(ring_tooltip).to_be_visible()
            day_tip = ring_tooltip.inner_text()
            assert "2026-" in day_tip and "credit" in day_tip and "个积分包" in day_tip
            page.mouse.move(0, 0)
            expect(ring_tooltip).to_be_hidden()
            day_segment.focus()
            expect(ring_tooltip).to_be_visible()
            assert "个积分包" in ring_tooltip.inner_text()
            page.locator(".quota-panel").screenshot(path=str(SCREENSHOTS / "v032-quota-ring-tooltip.png"))
            page.mouse.move(0, 0)
            later_segment = page.locator(".quota-buddy .ring-segment[data-label='较晚到期']")
            later_segment.focus()
            expect(ring_tooltip).to_contain_text("较晚到期")
            expect(ring_tooltip).to_contain_text("100 credit")
            refresh_segment = page.locator(".quota-buddy .ring-segment[data-label='周期刷新']")
            refresh_segment.focus()
            expect(ring_tooltip).to_contain_text("不是到期")
            expect(ring_tooltip).to_contain_text("50 credit")
            unknown_segment = page.locator(".quota-buddy .ring-segment[data-label='时间未知']")
            unknown_segment.focus()
            expect(ring_tooltip).to_contain_text("时间未知")
            expect(ring_tooltip).to_contain_text("340 credit")
            page.mouse.move(0, 0)

            # Narrow layout stacks the two halves cleanly.
            page.set_viewport_size({"width": 700, "height": 1100})
            page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            narrow_boxes = page.locator(".quota-split > section").evaluate_all(
                "(elements) => elements.map((element) => element.getBoundingClientRect())")
            assert narrow_boxes[1]["y"] > narrow_boxes[0]["y"] + narrow_boxes[0]["height"] - 1
            page.set_viewport_size({"width": 1800, "height": 1150})

            # Package dialog has exact per-package times, same names stay separate.
            action = page.locator(".quota-buddy .peer-action")
            expect(action).to_have_text("查看积分包")
            action.click()
            dialog = page.locator("#package-dialog")
            expect(dialog).to_be_visible()
            expect(page.locator("#package-dialog-summary")).to_contain_text("剩余 6,450.5 credit")
            expect(page.locator("#package-dialog-summary")).to_contain_text("总量 8,000")
            expect(page.locator("#package-dialog-summary")).to_contain_text("已用 1,549.5")
            expect(page.locator("#package-dialog-meta")).to_contain_text("已同步")
            expect(page.locator("#package-dialog-meta")).to_contain_text("WorkBuddy 账号")
            expect(page.locator("#package-dialog-body .buddy-package")).to_have_count(7)
            assert page.evaluate("document.activeElement.closest('dialog')?.id") == "package-dialog"
            packages = page.evaluate("""() => [...document.querySelectorAll('#package-dialog-body .buddy-package')].map((row) => ({
                name: row.dataset.name, expireAt: row.dataset.expireAt,
                refreshAt: row.dataset.refreshAt, text: row.innerText,
            }))""")
            assert len(packages) == 7
            plan_packages = [item for item in packages if item["name"] == "套餐基础积分"]
            assert len(plan_packages) == 3
            assert len({item["expireAt"] for item in plan_packages}) == 3
            assert any("到期时间未知" in item["text"] for item in packages)
            cycle_package = next(item for item in packages if item["name"] == "周期积分")
            assert "刷新" in cycle_package["text"] and "到期" not in cycle_package["text"]
            page.locator("#package-dialog").screenshot(path=str(SCREENSHOTS / "v032-quota-dialog.png"))
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            action.click()
            expect(dialog).to_be_visible()
            page.locator('#package-dialog [data-close]').first.click()
            expect(dialog).to_be_hidden()

            # Quota update while the dialog is open expands the ring with a residual segment.
            action.click()
            expect(dialog).to_be_visible()
            STATE["quota"]["buddy"]["remaining"] = 6500
            page.evaluate("async () => window.__emitMonitorState(await window.mockState())")
            expect(page.locator("#package-dialog-summary")).to_contain_text("6,500")
            expect(dialog).to_be_visible()
            expect(page.locator(".quota-buddy .ring-legend")).to_contain_text("其他余额")
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()

            # Known zero is distinct from unknown and still shows a ring.
            zero = deepcopy(KNOWN_BUDDY)
            zero.update({"remaining": 0, "total": 8000, "used": 8000, "frozen": 0, "packages": []})
            STATE["quota"]["buddy"] = zero
            page.reload()
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            expect(page.locator(".quota-buddy .quota-state")).to_have_text("已同步")
            expect(page.locator(".quota-buddy .ring-zero-note")).to_contain_text("剩余积分为 0（已知）")
            expect(page.locator(".quota-buddy .ring-segment")).to_have_count(0)

            # Many packages scroll inside the dialog, not in the split quota region.
            many = deepcopy(KNOWN_BUDDY)
            many["remaining"] = 310
            many["total"] = 310
            many["used"] = 0
            many["packages"] = [
                {"name": f"合成积分包 {index:02d}", "remaining": 10, "total": 10, "used": 0,
                 "expireAt": STAMP + 3600 + index * 3600, "refreshAt": None,
                 "showExpireAt": True, "showRefreshAt": False}
                for index in range(1, 32)
            ]
            STATE["quota"]["buddy"] = many
            page.reload()
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            page.locator(".quota-buddy .peer-action").click()
            expect(dialog).to_be_visible()
            expect(page.locator("#package-dialog-body .buddy-package")).to_have_count(31)
            body_size = page.locator("#package-dialog-body").evaluate(
                "(element) => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight})")
            assert body_size["scrollHeight"] > body_size["clientHeight"]
            body_scroll = page.locator("#package-dialog-body").evaluate(
                "(element) => { element.scrollTop = 120; return element.scrollTop; }")
            assert body_scroll > 0
            STATE["quota"]["buddy"]["remaining"] = 320
            page.evaluate("async () => window.__emitMonitorState(await window.mockState())")
            expect(page.locator("#package-dialog-summary")).to_contain_text("320")
            expect(dialog).to_be_visible()
            assert abs(page.locator("#package-dialog-body").evaluate(
                "(element) => element.scrollTop") - body_scroll) <= 1
            page.keyboard.press("Escape")

            # Package-detail failure keeps balance/message and removes the ring claim.
            package_error = deepcopy(KNOWN_BUDDY)
            package_error["packagesStatus"] = "error"
            package_error["packagesError"] = "合成积分包明细读取失败"
            STATE["quota"]["buddy"] = package_error
            page.reload()
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            expect(page.locator(".quota-buddy .quota-state")).to_have_text("已同步")
            package_text = page.locator(".quota-buddy").inner_text()
            assert "6,450.5" in package_text and "到期分布暂不可用" in package_text
            expect(page.locator(".quota-buddy .ring-segment")).to_have_count(0)
            page.locator(".quota-buddy .peer-action").click()
            expect(dialog).to_be_visible()
            expect(page.locator("#package-dialog-body")).to_contain_text("积分包明细暂不可用")
            expect(page.locator("#package-dialog-body")).to_contain_text("合成积分包明细读取失败")
            assert "到期" not in page.locator("#package-dialog-body").inner_text()
            expect(page.locator("#package-dialog-body .buddy-package")).to_have_count(0)
            page.keyboard.press("Escape")

            # Stale keeps the last credit summary but labels success, failure and frozen credit.
            stale = deepcopy(KNOWN_BUDDY)
            stale.update({"status": "stale", "frozen": 12.5,
                          "error": "合成额度刷新失败", "lastAttempt": STAMP - 300})
            STATE["quota"]["buddy"] = stale
            page.reload()
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            expect(page.locator(".quota-buddy .quota-state")).to_have_text("可能过期")
            stale_text = page.locator(".quota-buddy").inner_text()
            assert "6,450.5" in stale_text and "冻结 12.5" in stale_text
            assert "上次成功" in stale_text and "合成额度刷新失败" in stale_text

            # Auth failure clears plan, old values and the ring.
            failed = deepcopy(KNOWN_BUDDY)
            failed.update({"status": "error", "error": "账号授权已失效（合成）",
                           "lastAttempt": STAMP - 30})
            STATE["quota"]["buddy"] = failed
            page.reload()
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            expect(page.locator(".quota-buddy .quota-state")).to_have_text("读取失败")
            failed_text = page.locator(".quota-buddy").inner_text()
            assert "旧额度已清除" in failed_text and "账号授权已失效（合成）" in failed_text
            assert "6,450.5" not in failed_text
            expect(page.locator(".quota-buddy .peer-plan")).to_have_count(0)
            expect(page.locator(".quota-buddy .peer-action")).to_have_count(0)
            expect(page.locator(".quota-buddy .ring-segment")).to_have_count(0)

            # Unknown shows the safe error/login instruction and no invented values.
            STATE["quota"]["buddy"] = {
                "status": "unknown", "plan": "标准版", "remaining": None, "total": None,
                "used": None, "frozen": None, "unit": "credit", "packages": [],
                "packagesStatus": "known", "packagesError": None, "fetchedAt": None,
                "lastAttempt": STAMP - 10,
                "source": {"kind": "account", "label": "WorkBuddy 账号"},
                "error": "请重新登录 WorkBuddy 账号（合成）",
            }
            page.reload()
            expect(page.locator("#card-records")).to_have_text("7", timeout=15000)
            expect(page.locator(".quota-buddy .quota-state")).to_have_text("额度未知")
            unknown_text = page.locator(".quota-buddy").inner_text()
            assert "不显示估算值" in unknown_text
            assert "请重新登录 WorkBuddy 账号（合成）" in unknown_text
            assert "6,450.5" not in unknown_text and "%" not in unknown_text
            expect(page.locator(".quota-buddy .peer-plan")).to_have_count(0)
            expect(page.locator(".quota-buddy .peer-action")).to_have_count(0)
            expect(page.locator(".quota-buddy .ring-segment")).to_have_count(0)
            browser.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    run()
