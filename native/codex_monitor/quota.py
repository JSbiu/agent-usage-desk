"""Read subscription limits through Codex's account API; retain only quota fields."""
from datetime import datetime
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
from threading import Thread
import time

from . import __version__


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def snapshot(result, fetched_at=None, source="account"):
    buckets = result.get("rateLimitsByLimitId") or result.get("rate_limits_by_limit_id")
    if not isinstance(buckets, dict) or not buckets:
        legacy = result.get("rateLimits") or result.get("rate_limits")
        buckets = {"codex": legacy} if isinstance(legacy, dict) else {}
    limits = []
    for identifier, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        windows = []
        for kind in ("primary", "secondary"):
            raw = bucket.get(kind)
            if not isinstance(raw, dict):
                continue
            used = number(raw.get("usedPercent", raw.get("used_percent")))
            minutes = number(raw.get("windowDurationMins", raw.get("window_minutes")))
            reset = number(raw.get("resetsAt", raw.get("resets_at")))
            windows.append(dict(kind=kind, used=used,
                remaining=max(0, min(100, 100 - used)) if used is not None else None,
                minutes=minutes if minutes and minutes > 0 else None,
                resetAt=reset if reset and reset > 0 else None))
        if windows:
            limits.append(dict(id=str(bucket.get("limitId") or bucket.get("limit_id") or identifier)[:160],
                name=str(bucket.get("limitName") or bucket.get("limit_name") or identifier)[:160],
                plan=(bucket.get("planType") or bucket.get("plan_type"))
                    if isinstance(bucket.get("planType") or bucket.get("plan_type"), str) else None, windows=windows))
    if not limits:
        raise RuntimeError("账号暂未返回可显示的订阅额度。")
    return dict(buckets=limits, fetchedAt=fetched_at if fetched_at is not None else time.time(), source=source)


def executable():
    found = shutil.which("codex.exe" if os.name == "nt" else "codex")
    desktop = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if found:
        return found
    if desktop.is_file():
        return str(desktop)
    raise RuntimeError("未找到 Codex 程序，请安装 Codex 并登录订阅账号。")


def read_live(home, timeout=15, command=None):
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    process = subprocess.Popen([command or executable(), "app-server"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    messages = Queue()

    def receive():
        try:
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except (ValueError, UnicodeError):
                    continue
        finally:
            messages.put(None)

    reader = Thread(target=receive, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(method, params=None, identifier=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        if identifier is not None:
            message["id"] = identifier
        process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        process.stdin.flush()

    def response(identifier):
        while True:
            try:
                message = messages.get(timeout=max(0, deadline - time.monotonic()))
            except Empty as exc:
                raise RuntimeError("订阅额度读取超时，保留上次结果。") from exc
            if message is None:
                raise RuntimeError("Codex 账号接口已断开，请检查 Codex 登录状态。")
            if isinstance(message, dict) and message.get("id") == identifier:
                if message.get("error") or not isinstance(message.get("result"), dict):
                    raise RuntimeError("无法读取订阅额度，请确认 Codex 已登录订阅账号。")
                return message["result"]

    try:
        send("initialize", {"clientInfo": {"name": "codex_monitor", "title": "Codex Monitor", "version": __version__}}, 0)
        response(0)
        send("initialized", {})
        send("account/rateLimits/read", identifier=1)
        return snapshot(response(1))
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=1)
        process.stdout.close()


def read_logs(paths):
    """Fallback metadata from a bounded tail of the most recently changed logs."""
    latest = None
    for path in paths:
        try:
            with Path(path).open("rb") as stream:
                stream.seek(max(0, Path(path).stat().st_size - 1024 * 1024))
                lines = stream.read().splitlines()
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                    limits = event.get("payload", {}).get("rate_limits")
                    if not isinstance(limits, dict):
                        continue
                    at = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                    candidate = snapshot({"rateLimits": limits}, at, "log")
                    if latest is None or candidate["fetchedAt"] > latest["fetchedAt"]:
                        latest = candidate
                    break
                except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                    continue
        except OSError:
            continue
    return latest
