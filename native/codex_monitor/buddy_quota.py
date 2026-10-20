"""Read WorkBuddy personal quota and package expiry details.

The credential file is opened read-only for every query.  The access token is
only ever placed in an Authorization header for fixed official endpoints; it is
never cached, logged or returned.  This module deliberately has no login,
refresh, migration or fallback path: WorkBuddy owns the login lifecycle.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

API_URL = "https://www.workbuddy.cn/billing/meter/get-user-resource-summary"
PAID_URL = "https://www.workbuddy.cn/billing/meter/get-user-resource-paid-packages"
FREE_URL = "https://www.workbuddy.cn/billing/meter/get-user-resource-free-packages"
API_DOMAIN = "www.workbuddy.cn"
USER_AGENT = "CodexMonitor/0.3.3"
CREDENTIAL_PARTS = ("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")
CACHE_NAME = "buddy-quota.json"
CACHE_VERSION = 3
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CACHE_BYTES = 2 * 1024 * 1024
DETAIL_PAGE_SIZE = 200
DETAIL_MAX_PAGES = 10
DETAIL_MAX_RECORDS = DETAIL_PAGE_SIZE * DETAIL_MAX_PAGES
OVERALL_TIMEOUT = 25.0
CALL_TIMEOUT = 8.0
BEIJING = timezone(timedelta(hours=8))
SOURCE = {"kind": "account", "label": "WorkBuddy 账号"}

PACKAGE_CODE_MAP = {
    "bonus29": "TCACA_code_029_6wCGEWquYy",
    "bonusIntl": "TCACA_code_037_WxOD3MpI2o",
    "freeMonIntl": "TCACA_code_035_ArVxJcGDsm",
    "freeMon": "TCACA_code_008_cfWoLwvjU4",
    "bonus30": "TCACA_code_030_BjSt89qTvr",
    "proMon": "TCACA_code_002_AkiJS3ZHF5",
    "proTrialYear": "TCACA_code_040_mi9rCYg46x",
    "flagship": "TCACA_code_027_0FCGVA6vSa",
    "bonus28": "TCACA_code_028_NtpWi0jzXs",
    "youth": "TCACA_code_023_4xbGhMrE6q",
    "proTrialMon": "TCACA_code_039_KRcQj7wUat",
    "extra": "TCACA_code_009_0XmEQc2xOf",
    "gift": "TCACA_code_006_DbXS0lrypC",
    "extraIntl": "TCACA_code_036_lupO5WgNdG",
    "proYear": "TCACA_code_003_FAnt7lcmRT",
    "activity": "TCACA_code_007_nzdH5h4Nl0",
    "free": "TCACA_code_001_PqouKr6QWV",
    "proMonPlus": "TCACA_code_005_maRGyrHhw1",
    "advanced": "TCACA_code_026_BaESVICNoi",
    "extra38": "TCACA_code_038_OhvqZtiPKr",
}
PACKAGE_CODES = tuple(PACKAGE_CODE_MAP.values())
PAID_KINDS = frozenset(("proMon", "proMonPlus", "proYear", "youth",
                        "advanced", "flagship", "extra", "extra38", "extraIntl"))
STATIC_TO_KIND = {value: key for key, value in PACKAGE_CODE_MAP.items()}
PACKAGE_KINDS = frozenset(PACKAGE_CODE_MAP)
REFRESH_KINDS = frozenset((
    "proMon", "proMonPlus", "proYear", "youth", "advanced", "flagship",
    "bonus28", "freeMon", "proTrialMon", "proTrialYear", "bonusIntl",
    "freeMonIntl",
))
EXPIRE_KINDS = frozenset((
    "activity", "extra", "extra38", "extraIntl", "bonus29", "bonus30",
))

MISSING_MESSAGE = "未找到 WorkBuddy 登录信息，请在 WorkBuddy 中登录后重试。"
LOGOUT_MESSAGE = "WorkBuddy 已退出登录，请在 WorkBuddy 中登录后重试。"
INVALID_CREDENTIAL = "WorkBuddy 登录信息无法读取，请在 WorkBuddy 中重新登录。"
EXPIRED_MESSAGE = "WorkBuddy 登录已过期，请在 WorkBuddy 中重新登录。"
ENTERPRISE_MESSAGE = "暂不支持企业版 WorkBuddy 账号的额度查询，请使用个人账号。"
ACCOUNT_TYPE_MESSAGE = "暂不支持此 WorkBuddy 账号类型，请使用个人账号。"
DOMAIN_MESSAGE = "WorkBuddy 登录服务地址不受支持，请使用官方个人账号重新登录。"
AUTH_MESSAGE = "WorkBuddy 登录状态无效，请在 WorkBuddy 中重新登录。"
NETWORK_MESSAGE = "无法连接 WorkBuddy 额度服务，请稍后重试。"
STALE_MESSAGE = "WorkBuddy 额度暂时无法更新，当前显示上次结果。"
DATA_MESSAGE = "WorkBuddy 额度数据异常，暂时无法显示。"
REDIRECT_MESSAGE = "WorkBuddy 额度服务返回了重定向，已停止请求。"
UNKNOWN_MESSAGE = "WorkBuddy 额度暂时不可用。"
DETAILS_MESSAGE = "WorkBuddy 积分包明细暂时无法获取。"
DETAILS_MISMATCH = "WorkBuddy 积分包明细与总额不一致，暂时无法显示。"
DETAILS_CONFLICT = "WorkBuddy 积分包明细存在冲突，暂时无法显示。"
DETAILS_TRUNCATED = "WorkBuddy 积分包明细未完整返回，暂时无法显示。"
DETAILS_INCOMPLETE = "WorkBuddy 积分包明细缺少部分积分包，暂时无法显示。"
DETAILS_TIMEOUT = "WorkBuddy 积分包明细查询超时。"
SWITCH_MESSAGE = "WorkBuddy 账号已切换，请重新查询额度。"


class QuotaFailure(Exception):
    """Safe failure carrying only a user-facing Chinese message."""

    def __init__(self, status="error", message=DATA_MESSAGE, transient=False,
                 invalidate=False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.transient = transient
        self.invalidate = invalidate


QuotaError = QuotaFailure


def default_credential_path():
    base = os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base)
    elif os.name == "nt":
        root = Path.home() / "AppData" / "Local"
    else:
        root = Path.home() / ".local" / "share"
    return root.joinpath(*CREDENTIAL_PARTS)


def empty_snapshot(status="unknown", message=None, now=None):
    reason = message if message is not None else UNKNOWN_MESSAGE
    return dict(status=status, plan=None, remaining=None, total=None, used=None,
                frozen=None, unit="credit", groups=[], packages=[],
                packagesStatus="error", packagesError=reason,
                fetchedAt=None, lastAttempt=int(time.time() if now is None else now),
                source=dict(SOURCE), error=message)


def _decimal(value):
    """Return a finite, non-negative Decimal, rejecting bools and junk."""
    if type(value) is bool:
        raise QuotaFailure("error", DATA_MESSAGE)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise QuotaFailure("error", DATA_MESSAGE)
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            raise QuotaFailure("error", DATA_MESSAGE)
    elif type(value) in (int, float):
        if isinstance(value, float) and not math.isfinite(value):
            raise QuotaFailure("error", DATA_MESSAGE)
        number = Decimal(str(value))
    else:
        raise QuotaFailure("error", DATA_MESSAGE)
    if not number.is_finite() or number < 0:
        raise QuotaFailure("error", DATA_MESSAGE)
    return number


def _unit(value):
    if not isinstance(value, str):
        raise QuotaFailure("error", DATA_MESSAGE)
    if value.strip().lower() not in ("credit", "credits"):
        raise QuotaFailure("error", DATA_MESSAGE)
    return "credit"


def _text(value, limit=160):
    if not isinstance(value, str):
        raise QuotaFailure("error", DATA_MESSAGE)
    return value.strip()[:limit]


def _amounts(value):
    if not isinstance(value, dict):
        raise QuotaFailure("error", DATA_MESSAGE)
    return (_unit(value.get("CapacityUnit")),
            _decimal(value.get("CycleTotalCapacity")),
            _decimal(value.get("CycleRemainCapacity")),
            _decimal(value.get("CycleUsedCapacity")),
            _decimal(value.get("CycleFrozenCapacity")))


def _public_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise QuotaFailure("error", DATA_MESSAGE)
    return number


def _tolerance(value):
    number = Decimal(str(value))
    return max(Decimal("0.01"), abs(number) * Decimal("0.000000001"))


def _parse_payload(payload):
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise QuotaFailure("error", DATA_MESSAGE)
    if payload.get("code") != 0:
        raise QuotaFailure("error", DATA_MESSAGE)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QuotaFailure("error", DATA_MESSAGE)

    paid = data.get("IsPaidUser")
    if paid is not None and type(paid) is not bool:
        raise QuotaFailure("error", DATA_MESSAGE)
    plan = data.get("SubscriptionPackageName")
    if plan is not None:
        if not isinstance(plan, str):
            raise QuotaFailure("error", DATA_MESSAGE)
        plan = plan.strip()[:160] or None

    total_credits = _decimal(data.get("TotalCredits"))
    packages = data.get("Packages")
    if not isinstance(packages, list):
        raise QuotaFailure("error", DATA_MESSAGE)

    total = Decimal(0)
    remaining = Decimal(0)
    used = Decimal(0)
    frozen = Decimal(0)
    units = set()
    for package in packages:
        unit, package_total, package_remaining, package_used, package_frozen = _amounts(package)
        units.add(unit)
        total += package_total
        remaining += package_remaining
        used += package_used
        frozen += package_frozen
    if len(units) > 1:
        raise QuotaFailure("error", DATA_MESSAGE)
    unit = units.pop() if units else "credit"

    if not packages:
        if total_credits != 0:
            raise QuotaFailure("error", DATA_MESSAGE)
        total = remaining = used = frozen = Decimal(0)
    tolerance = _tolerance(total)
    if abs(remaining - total_credits) > tolerance:
        raise QuotaFailure("error", DATA_MESSAGE)
    if abs(remaining + used + frozen - total) > tolerance:
        raise QuotaFailure("error", DATA_MESSAGE)

    groups = []
    raw_groups = data.get("CreditGroups")
    if raw_groups is not None:
        if not isinstance(raw_groups, list):
            raise QuotaFailure("error", DATA_MESSAGE)
        for item in raw_groups:
            if not isinstance(item, dict):
                raise QuotaFailure("error", DATA_MESSAGE)
            kind = _text(item.get("Kind"))
            if not kind:
                raise QuotaFailure("error", DATA_MESSAGE)
            name = item.get("Name")
            if name is None or name == "":
                name = kind
            elif not isinstance(name, str):
                raise QuotaFailure("error", DATA_MESSAGE)
            name = (name.strip() or kind)[:160]
            group_unit, group_total, group_remaining, group_used, group_frozen = _amounts(item)
            if group_unit != unit:
                raise QuotaFailure("error", DATA_MESSAGE)
            if abs(group_remaining + group_used + group_frozen - group_total) > _tolerance(group_total):
                raise QuotaFailure("error", DATA_MESSAGE)
            groups.append(dict(kind=kind, name=name,
                               remaining=_public_number(group_remaining),
                               total=_public_number(group_total),
                               used=_public_number(group_used),
                               frozen=_public_number(group_frozen)))

    return dict(plan=plan, remaining=_public_number(remaining),
                total=_public_number(total), used=_public_number(used),
                frozen=_public_number(frozen), unit=unit, groups=groups)


def _epoch_seconds(value):
    if not math.isfinite(value) or value <= 0:
        return None
    whole = int(value)
    return whole if value == whole else value


def _epoch(value):
    """Accept epoch milliseconds/date strings; return epoch seconds or None."""
    if type(value) is bool or value is None:
        return None
    if type(value) in (int, float):
        if not math.isfinite(value) or value <= 0:
            return None
        try:
            return _epoch_seconds(value / 1000.0)
        except OverflowError:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        try:
            milliseconds = int(text)
            return _epoch_seconds(milliseconds / 1000.0)
        except (ValueError, OverflowError):
            return None
    if " " not in text and "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    try:
        return _epoch_seconds(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _package_kind(code):
    if not isinstance(code, str):
        return None
    text = code.strip()
    if not text:
        return None
    return STATIC_TO_KIND.get(text, text if text in PACKAGE_KINDS else None)


def _used_amount(item, total, remaining):
    raw = item.get("CycleCapacityUsedPrecise")
    if raw is None or raw == "":
        return max(Decimal(0), total - remaining)
    return _decimal(raw)


def _parse_package(item):
    if not isinstance(item, dict):
        raise QuotaFailure("error", DETAILS_MESSAGE)
    name = item.get("PackageName")
    if isinstance(name, str) and name.strip():
        name = name.strip()[:160]
    else:
        name = "积分包"
    _unit(item.get("CapacityUnit"))
    capacity_type = item.get("CapacityType")
    if capacity_type is not None and type(capacity_type) is not int:
        raise QuotaFailure("error", DETAILS_MESSAGE)

    if capacity_type == 4:
        slices = item.get("SlicePeriodUsageDetails")
        if slices is not None and not isinstance(slices, list):
            raise QuotaFailure("error", DETAILS_MESSAGE)
        if slices:
            current = slices[0]
            if not isinstance(current, dict):
                raise QuotaFailure("error", DETAILS_MESSAGE)
            if (current.get("SlicePeriodCapacitySizePrecise") is not None
                    and current.get("SlicePeriodCapacityRemainPrecise") is not None):
                total = _decimal(current.get("SlicePeriodCapacitySizePrecise"))
                remaining = _decimal(current.get("SlicePeriodCapacityRemainPrecise"))
                used = max(Decimal(0), total - remaining)
            else:
                total = _decimal(item.get("CycleCapacitySizePrecise"))
                remaining = _decimal(item.get("CycleCapacityRemainPrecise"))
                used = _used_amount(item, total, remaining)
        else:
            total = _decimal(item.get("CycleCapacitySizePrecise"))
            remaining = _decimal(item.get("CycleCapacityRemainPrecise"))
            used = _used_amount(item, total, remaining)
    else:
        total = _decimal(item.get("CycleCapacitySizePrecise"))
        remaining = _decimal(item.get("CycleCapacityRemainPrecise"))
        used = _used_amount(item, total, remaining)

    if remaining > total or abs(total - remaining - used) > _tolerance(total):
        raise QuotaFailure("error", DETAILS_MESSAGE)
    kind = _package_kind(item.get("PackageCode"))
    cycle_end = _epoch(item.get("CycleEndTime"))
    if kind == "free":
        expire_at = cycle_end
        refresh_at = None
        show_expire = True
        show_refresh = False
    else:
        expire_at = _epoch(item.get("DeductionEndTime"))
        if expire_at is None:
            expire_at = _epoch(item.get("ExpiredTime"))
        if expire_at is None:
            expire_at = cycle_end
        refresh_at = cycle_end
        if kind in REFRESH_KINDS:
            show_expire, show_refresh = False, True
        elif kind in EXPIRE_KINDS:
            show_expire, show_refresh = True, False
        else:
            show_expire, show_refresh = True, True

    resource = item.get("ResourceId")
    if isinstance(resource, str) and resource.strip():
        resource = resource.strip()
    else:
        resource = None
    package = dict(name=name, remaining=_public_number(remaining),
                   total=_public_number(total), used=_public_number(used),
                   expireAt=expire_at, refreshAt=refresh_at,
                   showExpireAt=show_expire, showRefreshAt=show_refresh)
    return package, resource


def _effective_time(package):
    values = []
    if package["showExpireAt"] and package["expireAt"] is not None:
        values.append(package["expireAt"])
    if package["showRefreshAt"] and package["refreshAt"] is not None:
        values.append(package["refreshAt"])
    return min(values) if values else None


def _parse_details(accounts, summary_remaining, eligible_codes=None):
    packages = []
    seen = {}
    present_codes = set()
    for item in accounts:
        if not isinstance(item, dict):
            raise QuotaFailure("error", DETAILS_MESSAGE)
        # The account summary already excludes lower-tier plan allowances.
        # Detail endpoints still return those packages (e.g. freeMon alongside
        # a paid plan); only packages included in the authoritative pool count.
        if eligible_codes is not None:
            code = item.get("PackageCode")
            if not isinstance(code, str) or code not in eligible_codes:
                continue
            # Coverage is tracked before parsing/deduplication so even a
            # depleted zero-remaining package must appear in the detail pages.
            present_codes.add(code)
        package, resource = _parse_package(item)
        if resource is not None:
            if resource in seen:
                if seen[resource] != package:
                    raise QuotaFailure("error", DETAILS_CONFLICT)
                continue
            seen[resource] = package
        packages.append(package)

    if eligible_codes is not None and not eligible_codes.issubset(present_codes):
        raise QuotaFailure("error", DETAILS_INCOMPLETE)

    detail_total = sum((Decimal(str(package["remaining"])) for package in packages),
                       Decimal(0))
    expected = Decimal(str(summary_remaining))
    if abs(detail_total - expected) > _tolerance(expected):
        raise QuotaFailure("error", DETAILS_MISMATCH)
    packages.sort(key=lambda package: (
        _effective_time(package) is None,
        _effective_time(package) if _effective_time(package) is not None else 0,
        package["name"]))
    return packages


def _credential(path, now):
    marker = Path(str(path) + ".logged-out")
    try:
        if marker.exists():
            raise QuotaFailure("unknown", LOGOUT_MESSAGE)
        info = path.stat()
    except QuotaFailure:
        raise
    except FileNotFoundError:
        raise QuotaFailure("unknown", MISSING_MESSAGE)
    except OSError:
        raise QuotaFailure("unknown", MISSING_MESSAGE)
    if not path.is_file() or info.st_size > MAX_CREDENTIAL_BYTES:
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
    except OSError:
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    if not isinstance(value, dict):
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    account, auth = value.get("account"), value.get("auth")
    if not isinstance(account, dict) or not isinstance(auth, dict):
        raise QuotaFailure("error", INVALID_CREDENTIAL)

    enterprise = account.get("enterpriseId")
    if enterprise not in (None, "", 0, False):
        raise QuotaFailure("error", ENTERPRISE_MESSAGE)
    if account.get("type") != "personal":
        raise QuotaFailure("error", ACCOUNT_TYPE_MESSAGE)

    uid = account.get("uid")
    if (not isinstance(uid, str) or not uid.strip() or len(uid) > 256
            or any(char in uid for char in "\r\n\x00")):
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    uid = uid.strip()

    domain = auth.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    if domain.strip().lower() != API_DOMAIN:
        raise QuotaFailure("error", DOMAIN_MESSAGE)

    token = auth.get("accessToken")
    if (not isinstance(token, str) or not token or len(token) > 8192
            or any(char in token for char in "\r\n\x00")):
        raise QuotaFailure("error", INVALID_CREDENTIAL)

    expires = auth.get("expiresAt")
    if type(expires) is bool or type(expires) not in (int, float):
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    if not math.isfinite(expires) or expires <= 0:
        raise QuotaFailure("error", INVALID_CREDENTIAL)
    if expires <= int(now * 1000):
        raise QuotaFailure("error", EXPIRED_MESSAGE)

    identity = hashlib.sha256(
        b"workbuddy\0" + uid.encode("utf-8") + b"\0" + API_DOMAIN.encode("ascii")
    ).hexdigest()
    return dict(identity=identity, uid=uid, token=token)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        try:
            stream.close()
        except Exception:
            pass
        raise QuotaFailure("error", REDIRECT_MESSAGE)


def _transport(url, data, headers, timeout):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.getcode(), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            body = b""
        finally:
            exc.close()
        return exc.code, body
    except QuotaFailure:
        raise
    except (urllib.error.URLError, TimeoutError, OSError,
            http.client.HTTPException) as exc:
        raise QuotaFailure("error", NETWORK_MESSAGE, transient=True) from exc


class BuddyQuotaReader:
    """Query fixed WorkBuddy summary and package endpoints when called."""

    def __init__(self, cache=None, credential_path=None, transport=None,
                 timeout=8, now=None, monotonic=None):
        self.cache = Path(cache) if cache is not None else None
        self.credential_path = (Path(credential_path) if credential_path is not None
                                else default_credential_path())
        self.transport = transport or _transport
        self.timeout = timeout
        self.now = now or time.time
        self.monotonic = monotonic or time.monotonic
        self._cached = None

    def __call__(self):
        return self.read()

    def read(self):
        now = int(self.now())
        try:
            credential = _credential(self.credential_path, now)
        except QuotaFailure as exc:
            self._clear()
            return empty_snapshot(exc.status, exc.message, now)

        deadline = self.monotonic() + OVERALL_TIMEOUT
        cached = self._load()
        if cached is not None and cached.get("identity") != credential["identity"]:
            self._clear()
            cached = None
        try:
            summary, eligible_codes = self._fetch_summary(credential, deadline)
        except QuotaFailure as exc:
            return self._summary_failed(exc, credential, cached, now)

        packages = []
        packages_status = "error"
        packages_error = None
        try:
            accounts = self._fetch_details(credential, now, deadline)
            packages = _parse_details(accounts, summary["remaining"], eligible_codes)
            packages_status = "known"
        except QuotaFailure as exc:
            if exc.invalidate:
                self._clear()
                return empty_snapshot("error", AUTH_MESSAGE, now)
            packages_error = exc.message

        switched = self._verify_identity(credential)
        if switched is not None:
            return switched

        result = dict(summary, status="known", packages=packages,
                      packagesStatus=packages_status, packagesError=packages_error,
                      fetchedAt=now, lastAttempt=now, source=dict(SOURCE), error=None)
        self._save(credential["identity"], result)
        return result

    def _summary_failed(self, failure, credential, cached, now):
        if failure.transient and cached is not None:
            switched = self._verify_identity(credential)
            if switched is not None:
                return switched
            value = dict(cached["data"])
            value.update(status="stale", lastAttempt=now, error=STALE_MESSAGE,
                         source=dict(SOURCE))
            self._remember(credential["identity"], cached["data"])
            return value
        self._clear()
        return empty_snapshot(failure.status, failure.message, now)

    def _verify_identity(self, credential):
        try:
            current = _credential(self.credential_path, int(self.now()))
        except QuotaFailure as exc:
            self._clear()
            return empty_snapshot(exc.status, exc.message, int(self.now()))
        if current["identity"] != credential["identity"]:
            self._clear()
            return empty_snapshot("unknown", SWITCH_MESSAGE, int(self.now()))
        return None

    def _timeout(self, deadline, message):
        remaining = deadline - self.monotonic()
        limit = min(CALL_TIMEOUT, self.timeout)
        if remaining <= 0 or limit <= 0:
            raise QuotaFailure("error", message, transient=True)
        return min(limit, remaining)

    def _headers(self, credential):
        return {
            "Authorization": "Bearer " + credential["token"],
            "X-User-Id": credential["uid"],
            "X-Domain": API_DOMAIN,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "zh",
            "User-Agent": USER_AGENT,
        }

    def _post(self, url, body, credential, deadline, error_message):
        timeout = self._timeout(deadline, error_message)
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            status, raw = self.transport(url, data, self._headers(credential),
                                         timeout)
        except QuotaFailure:
            raise
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as exc:
            raise QuotaFailure("error", NETWORK_MESSAGE, transient=True) from exc
        except Exception as exc:
            raise QuotaFailure("error", UNKNOWN_MESSAGE) from exc
        if status in (401, 403):
            raise QuotaFailure("error", AUTH_MESSAGE, invalidate=True)
        if status in (408, 425, 429) or 500 <= status <= 599:
            raise QuotaFailure("error", NETWORK_MESSAGE, transient=True)
        if 300 <= status <= 399:
            raise QuotaFailure("error", REDIRECT_MESSAGE)
        if status != 200:
            raise QuotaFailure("error", error_message)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
            raise QuotaFailure("error", error_message)
        try:
            value = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise QuotaFailure("error", error_message)
        if not isinstance(value, dict):
            raise QuotaFailure("error", error_message)
        return value

    def _fetch_summary(self, credential, deadline):
        payload = self._post(API_URL, {}, credential, deadline, DATA_MESSAGE)
        summary = _parse_payload(payload)
        codes = set()
        for package in payload["data"]["Packages"]:
            code = package.get("PackageCode")
            if not isinstance(code, str) or not code:
                raise QuotaFailure("error", DATA_MESSAGE)
            codes.add(code)
        return summary, codes

    def _details_body(self, kind, page, now):
        body = {"PageNumber": page, "PageSize": DETAIL_PAGE_SIZE,
                "PackageCodes": [code for name, code in PACKAGE_CODE_MAP.items()
                                 if (name in PAID_KINDS) == (kind == "paid")],
                "Status": [0, 3]}
        if kind == "paid":
            body["NeedRenewInfo"] = True
        else:
            day = datetime.fromtimestamp(now, BEIJING)
            body["SlicePeriodStartTime"] = day.strftime("%Y-%m-%d 00:00:00")
            body["SlicePeriodEndTime"] = day.strftime("%Y-%m-%d 23:59:59")
        return body

    def _fetch_pages(self, credential, url, kind, now, deadline):
        accounts = []
        total = None
        page = 1
        while page <= DETAIL_MAX_PAGES:
            payload = self._post(url, self._details_body(kind, page, now),
                                 credential, deadline, DETAILS_MESSAGE)
            if (type(payload.get("code")) is not int
                    or payload.get("code") != 0
                    or not isinstance(payload.get("data"), dict)):
                raise QuotaFailure("error", DETAILS_MESSAGE)
            data = payload["data"]
            page_total = data.get("TotalCount")
            page_accounts = data.get("Accounts")
            if (type(page_total) is not int or page_total < 0
                    or not isinstance(page_accounts, list)):
                raise QuotaFailure("error", DETAILS_TRUNCATED)
            if len(page_accounts) > DETAIL_PAGE_SIZE:
                raise QuotaFailure("error", DETAILS_TRUNCATED)
            if total is None:
                total = page_total
            elif total != page_total:
                raise QuotaFailure("error", DETAILS_TRUNCATED)
            if total > DETAIL_MAX_RECORDS:
                raise QuotaFailure("error", DETAILS_TRUNCATED)
            if len(accounts) + len(page_accounts) > total:
                raise QuotaFailure("error", DETAILS_TRUNCATED)
            accounts.extend(page_accounts)
            if len(accounts) == total:
                return accounts
            if not page_accounts:
                raise QuotaFailure("error", DETAILS_TRUNCATED)
            page += 1
        raise QuotaFailure("error", DETAILS_TRUNCATED)

    def _fetch_details(self, credential, now, deadline):
        accounts = []
        for kind, url in (("paid", PAID_URL), ("free", FREE_URL)):
            accounts.extend(self._fetch_pages(credential, url, kind, now, deadline))
        return accounts

    def _cache_path(self):
        return self.cache / CACHE_NAME if self.cache is not None else None

    @staticmethod
    def _core(data):
        return {key: data[key] for key in
                ("plan", "remaining", "total", "used", "frozen", "unit",
                 "groups", "packages", "packagesStatus", "packagesError",
                 "fetchedAt")}

    def _remember(self, identity, data):
        self._cached = {"identity": identity, "data": self._core(data)}

    @staticmethod
    def _valid_cache(value):
        if not isinstance(value, dict) or value.get("version") != CACHE_VERSION:
            return False
        if not isinstance(value.get("identity"), str):
            return False
        data = value.get("data")
        if not isinstance(data, dict):
            return False
        for key in ("remaining", "total", "used", "frozen", "fetchedAt"):
            number = data.get(key)
            if type(number) not in (int, float) or not math.isfinite(number):
                return False
        if data.get("plan") is not None and not isinstance(data.get("plan"), str):
            return False
        if data.get("unit") != "credit" or not isinstance(data.get("groups"), list):
            return False
        if "packages" not in data:
            data["packages"] = []
            data["packagesStatus"] = "error"
            data["packagesError"] = DETAILS_MESSAGE
        packages = data.get("packages")
        if not isinstance(packages, list):
            return False
        for package in packages:
            if not isinstance(package, dict) or not isinstance(package.get("name"), str):
                return False
            for key in ("remaining", "total", "used"):
                number = package.get(key)
                if type(number) not in (int, float) or not math.isfinite(number):
                    return False
                if number < 0:
                    return False
            for key in ("expireAt", "refreshAt"):
                number = package.get(key)
                if number is not None and (type(number) not in (int, float)
                                           or not math.isfinite(number)
                                           or number < 0):
                    return False
            if type(package.get("showExpireAt")) is not bool:
                return False
            if type(package.get("showRefreshAt")) is not bool:
                return False
        if data.get("packagesStatus") not in ("known", "error"):
            return False
        if data.get("packagesError") is not None and not isinstance(
                data.get("packagesError"), str):
            return False
        return True

    def _load(self):
        if self._cached is not None:
            return self._cached
        path = self._cache_path()
        if path is None:
            return None
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_CACHE_BYTES:
                return None
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        if not isinstance(value, dict) or value.get("version") != CACHE_VERSION:
            return None
        if not self._valid_cache(value):
            return None
        self._cached = value
        return value

    def _save(self, identity, data):
        path = self._cache_path()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
                temporary.write_text(json.dumps(
                    {"version": CACHE_VERSION, "identity": identity,
                     "data": self._core(data)},
                    ensure_ascii=False, allow_nan=False), encoding="utf-8")
                os.replace(str(temporary), str(path))
            except (OSError, ValueError):
                pass
        self._remember(identity, data)

    def _clear(self):
        self._cached = None
        path = self._cache_path()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def read_buddy_quota(cache=None, credential_path=None, transport=None,
                     timeout=8, now=None):
    return BuddyQuotaReader(cache, credential_path, transport, timeout, now).read()
