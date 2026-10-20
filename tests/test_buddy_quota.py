"""Offline checks for WorkBuddy quota, package details and identity cache."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from codex_monitor.buddy_quota import (API_DOMAIN, API_URL, BEIJING, FREE_URL,
                                      PACKAGE_CODES, PACKAGE_CODE_MAP, PAID_URL,
                                      USER_AGENT, BuddyQuotaReader)
from codex_monitor.service import Service

TOKEN = "fictional-access-token"
UID = "user-fictional"
PRO_MON = PACKAGE_CODE_MAP["proMon"]
ACTIVITY = PACKAGE_CODE_MAP["activity"]
FREE = PACKAGE_CODE_MAP["free"]


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def payload(total="8000", remaining="6450.5", used="1549.5", frozen="0",
            packages=True, groups=True):
    value = {
        "code": 0,
        "data": {
            "TotalCredits": remaining,
            "SubscriptionPackageName": "标准版",
            "IsPaidUser": True,
            "Packages": ([{
                "PackageCode": PRO_MON,
                "CycleTotalCapacity": total,
                "CycleRemainCapacity": remaining,
                "CycleUsedCapacity": used,
                "CycleFrozenCapacity": frozen,
                "CapacityUnit": "credits",
                "TotalCount": 1,
            }] if packages else []),
        },
    }
    if groups and packages:
        value["data"]["CreditGroups"] = [{
            "Kind": "plan_base",
            "Name": "套餐基础积分",
            "CycleTotalCapacity": total,
            "CycleRemainCapacity": remaining,
            "CycleUsedCapacity": used,
            "CycleFrozenCapacity": frozen,
            "CapacityUnit": "credits",
            "TotalCount": 1,
        }]
    return value


def details_page(accounts, total=None):
    return {"code": 0, "data": {"Accounts": accounts,
                                "TotalCount": len(accounts) if total is None else total}}


def account(name="套餐基础积分", code=PRO_MON, remaining="6450.5", total="8000",
            used="1549.5", resource="fictional-resource", capacity_type=1,
            cycle_end="2026-11-01 09:59:59", deduction_end=None,
            expired="", slice=None, extra=None):
    value = {
        "ResourceId": resource,
        "PackageCode": code,
        "PackageName": name,
        "Status": 0,
        "CapacityType": capacity_type,
        "CapacityUnit": "credits",
        "CycleCapacitySizePrecise": total,
        "CycleCapacityRemainPrecise": remaining,
        "CycleCapacityUsedPrecise": used,
        "CycleStartTime": "2026-10-01 10:00:00",
        "CycleEndTime": cycle_end,
        "DeductionStartTime": 1790000000000,
        "ExpiredTime": expired,
    }
    if deduction_end is not None:
        value["DeductionEndTime"] = deduction_end
    if slice is not None:
        value["SlicePeriodUsageDetails"] = slice
    if extra:
        value.update(extra)
    return value


def beijing_timestamp(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=BEIJING).timestamp())


class FakeTransport:
    def __init__(self, summary=None, paid=None, free=None, handler=None):
        self.summary = summary
        self.paid = paid
        self.free = free
        self.handler = handler
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append(dict(url=url, data=data, headers=dict(headers),
                               timeout=timeout))
        if self.handler is not None:
            result = self.handler(url, data, headers, timeout)
            if isinstance(result, Exception):
                raise result
            return result
        if url == API_URL:
            response = self.summary if self.summary is not None else payload()
        elif url == PAID_URL:
            response = (self.paid if self.paid is not None
                        else details_page([account()]))
        elif url == FREE_URL:
            response = self.free if self.free is not None else details_page([])
        else:
            response = RuntimeError("unexpected URL")
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, encode(response)


class Clock:
    def __init__(self, value=1_700_000_000):
        self.value = value

    def __call__(self):
        return self.value


class BuddyQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.credential = self.root / "auth" / "workbuddy-desktop.info"
        self.clock = Clock()
        self.transport = FakeTransport()
        self.write_credential()

    def tearDown(self):
        self.temp.cleanup()

    def write_credential(self, *, uid=UID, domain=API_DOMAIN, token=TOKEN,
                         expires=None, account_type="personal", enterprise=None,
                         raw=None):
        if raw is not None:
            self.credential.parent.mkdir(parents=True, exist_ok=True)
            self.credential.write_bytes(raw)
            return
        account = {"uid": uid, "type": account_type}
        if enterprise is not None:
            account["enterpriseId"] = enterprise
        value = {"account": account,
                 "auth": {"accessToken": token, "domain": domain,
                          "expiresAt": expires if expires is not None
                          else (self.clock.value + 3600) * 1000}}
        self.credential.parent.mkdir(parents=True, exist_ok=True)
        self.credential.write_text(json.dumps(value), encoding="utf-8")

    def reader(self):
        return BuddyQuotaReader(self.cache, self.credential,
                                transport=self.transport, timeout=3,
                                now=self.clock)

    def test_reads_credential_summary_and_package_requests(self):
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["plan"], "标准版")
        self.assertEqual((result["remaining"], result["total"], result["used"],
                          result["frozen"], result["unit"]),
                         (6450.5, 8000.0, 1549.5, 0.0, "credit"))
        self.assertEqual(result["packagesStatus"], "known")
        self.assertIsNone(result["packagesError"])
        self.assertEqual(len(result["packages"]), 1)
        package = result["packages"][0]
        self.assertEqual(set(package), {"name", "remaining", "total", "used",
                                        "expireAt", "refreshAt", "showExpireAt",
                                        "showRefreshAt"})
        self.assertEqual((package["remaining"], package["total"], package["used"]),
                         (6450.5, 8000.0, 1549.5))
        self.assertFalse(package["showExpireAt"])
        self.assertTrue(package["showRefreshAt"])
        self.assertEqual(package["refreshAt"], beijing_timestamp("2026-11-01 09:59:59"))
        self.assertEqual(result["groups"], [dict(
            kind="plan_base", name="套餐基础积分", remaining=6450.5,
            total=8000.0, used=1549.5, frozen=0.0)])
        self.assertEqual(result["fetchedAt"], self.clock.value)
        self.assertEqual(result["lastAttempt"], self.clock.value)
        self.assertIsNone(result["error"])
        self.assertEqual(result["source"], {"kind": "account", "label": "WorkBuddy 账号"})

        self.assertEqual([call["url"] for call in self.transport.calls],
                         [API_URL, PAID_URL, FREE_URL])
        summary_call = self.transport.calls[0]
        self.assertEqual(summary_call["data"], b"{}")
        self.assertLessEqual(summary_call["timeout"], 3)
        headers = summary_call["headers"]
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["X-User-Id"], UID)
        self.assertEqual(headers["X-Domain"], "www.workbuddy.cn")
        self.assertEqual(headers["User-Agent"], "CodexMonitor/0.3.3")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept-Language"], "zh")

        paid = json.loads(self.transport.calls[1]["data"])
        self.assertEqual((paid["PageNumber"], paid["PageSize"]), (1, 200))
        self.assertIn(PRO_MON, paid["PackageCodes"])
        self.assertNotIn(ACTIVITY, paid["PackageCodes"])
        self.assertEqual(paid["Status"], [0, 3])
        self.assertIs(paid["NeedRenewInfo"], True)
        free = json.loads(self.transport.calls[2]["data"])
        self.assertIn(ACTIVITY, free["PackageCodes"])
        self.assertNotIn(PRO_MON, free["PackageCodes"])
        self.assertEqual(set(paid["PackageCodes"]) | set(free["PackageCodes"]), set(PACKAGE_CODES))
        self.assertFalse(set(paid["PackageCodes"]) & set(free["PackageCodes"]))
        self.assertNotIn("NeedRenewInfo", free)
        day = datetime.fromtimestamp(self.clock.value, BEIJING)
        self.assertEqual(free["SlicePeriodStartTime"], day.strftime("%Y-%m-%d 00:00:00"))
        self.assertEqual(free["SlicePeriodEndTime"], day.strftime("%Y-%m-%d 23:59:59"))

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn(UID, serialized)
        cache_text = (self.cache / "buddy-quota.json").read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, cache_text)
        self.assertNotIn(UID, cache_text)

    def test_packages_are_authoritative_and_groups_are_not_recounted(self):
        first = {
            "PackageCode": "one", "CycleTotalCapacity": "5000",
            "CycleRemainCapacity": "4000", "CycleUsedCapacity": "1000",
            "CycleFrozenCapacity": "0", "CapacityUnit": "credits", "TotalCount": 1}
        second = {
            "PackageCode": "two", "CycleTotalCapacity": "3000",
            "CycleRemainCapacity": "2450.5", "CycleUsedCapacity": "549.5",
            "CycleFrozenCapacity": "0", "CapacityUnit": "credit", "TotalCount": 1}
        value = payload()
        value["data"]["Packages"] = [first, second]
        value["data"]["CreditGroups"] = [{
            "Kind": "plan_base", "Name": "套餐基础积分",
            "CycleTotalCapacity": "5000", "CycleRemainCapacity": "4000",
            "CycleUsedCapacity": "1000", "CycleFrozenCapacity": "0",
            "CapacityUnit": "credits", "TotalCount": 1}]
        self.transport = FakeTransport(summary=value, paid=details_page([
            account(code="one", resource="one", total="5000", remaining="4000", used="1000"),
            account(code="two", resource="two", total="3000", remaining="2450.5", used="549.5")]))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual((result["remaining"], result["total"], result["used"]),
                         (6450.5, 8000.0, 1549.5))
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["packagesStatus"], "known")

    def test_summary_excludes_superseded_free_plan_from_available_packages(self):
        paid = account()
        superseded = account(code=PACKAGE_CODE_MAP["freeMon"], resource="superseded",
                             name="已被付费档位替代", total="500", remaining="500", used="0")
        self.transport = FakeTransport(paid=details_page([paid]), free=details_page([superseded]))
        result = self.reader()()
        self.assertEqual(result["packagesStatus"], "known")
        self.assertEqual(len(result["packages"]), 1)
        self.assertEqual(result["packages"][0]["remaining"], result["remaining"])

    def test_impossible_package_amounts_are_not_shown_as_valid_details(self):
        for item in (account(total="100", remaining="6450.5", used="0"),
                     account(total="8000", remaining="6450.5", used="0")):
            with self.subTest(item=item["CycleCapacitySizePrecise"]):
                self.transport = FakeTransport(paid=details_page([item]))
                result = self.reader()()
                self.assertEqual(result["status"], "known")
                self.assertEqual(result["packagesStatus"], "error")
                self.assertEqual(result["packages"], [])

    def test_missing_zero_eligible_code_marks_packages_error(self):
        summary = payload(total="0", remaining="0", used="0", frozen="0")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([]), free=details_page([]))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["remaining"], 0.0)
        self.assertEqual(result["packagesStatus"], "error")
        self.assertEqual(result["packages"], [])
        self.assertTrue(result["packagesError"])

    def test_empty_packages_zero_is_known_but_bad_sums_are_unavailable(self):
        zero = payload(remaining="0", packages=False, groups=False)
        zero["data"]["SubscriptionPackageName"] = "免费版"
        self.transport = FakeTransport(summary=zero,
                                       paid=details_page([]), free=details_page([]))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual((result["remaining"], result["total"], result["used"],
                          result["frozen"]), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["packages"], [])
        self.assertEqual(result["packagesStatus"], "known")

        bad_values = []
        missing_packages = payload()
        missing_packages["data"].pop("Packages")
        bad_values.append(missing_packages)
        missing_total = payload()
        missing_total["data"].pop("TotalCredits")
        bad_values.append(missing_total)
        mismatch = payload()
        mismatch["data"]["TotalCredits"] = "1"
        bad_values.append(mismatch)
        negative = payload()
        negative["data"]["Packages"][0]["CycleRemainCapacity"] = "-1"
        bad_values.append(negative)
        boolean = payload()
        boolean["data"]["Packages"][0]["CycleUsedCapacity"] = True
        bad_values.append(boolean)
        unit = payload()
        unit["data"]["Packages"][0]["CapacityUnit"] = "tokens"
        bad_values.append(unit)
        nan = payload()
        nan["data"]["Packages"][0]["CycleTotalCapacity"] = "NaN"
        bad_values.append(nan)
        for index, bad in enumerate(bad_values):
            with self.subTest(index=index):
                reader = BuddyQuotaReader(self.cache, self.credential,
                                          transport=FakeTransport(summary=bad),
                                          now=self.clock)
                result = reader()
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["remaining"])
                self.assertEqual(result["groups"], [])
                self.assertEqual(result["packages"], [])
                self.assertEqual(result["packagesStatus"], "error")

    def test_missing_logout_invalid_enterprise_domain_and_expiry_fail_closed(self):
        missing = self.reader()
        self.credential.unlink()
        self.assertEqual(missing()["status"], "unknown")

        self.write_credential()
        Path(str(self.credential) + ".logged-out").write_text("", encoding="utf-8")
        self.assertEqual(self.reader()()["status"], "unknown")
        Path(str(self.credential) + ".logged-out").unlink()

        self.write_credential(raw=b"{not-json")
        self.assertEqual(self.reader()()["status"], "error")

        self.write_credential(enterprise="fictional-enterprise")
        result = self.reader()()
        self.assertEqual(result["status"], "error")
        self.assertIn("企业", result["error"])

        self.write_credential(domain="evil.example")
        result = self.reader()()
        self.assertEqual(result["status"], "error")
        self.assertIn("服务地址", result["error"])

        self.write_credential(account_type="team")
        self.assertEqual(self.reader()()["status"], "error")

        self.write_credential(expires=(self.clock.value - 1) * 1000)
        result = self.reader()()
        self.assertEqual(result["status"], "error")
        self.assertIn("过期", result["error"])
        self.assertEqual(self.transport.calls, [])

    def test_marker_is_logged_out_not_legacy_logout(self):
        Path(str(self.credential) + ".logged-out").write_text("", encoding="utf-8")
        self.assertEqual(self.reader()()["status"], "unknown")
        self.assertEqual(self.transport.calls, [])

        Path(str(self.credential) + ".logged-out").unlink()
        Path(str(self.credential) + ".logout").write_text("", encoding="utf-8")
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertTrue(self.transport.calls)

    def test_same_identity_transient_failure_is_stale_but_switch_drops_it(self):
        first = self.reader()()
        self.assertEqual(first["status"], "known")
        old_fetched = first["fetchedAt"]

        self.clock.value += 60
        failing = self.reader()
        failing.transport = FakeTransport(summary=OSError("SECRET raw network detail"))
        stale = failing()
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["remaining"], first["remaining"])
        self.assertEqual(stale["packages"], first["packages"])
        self.assertEqual(stale["packagesStatus"], "known")
        self.assertEqual(stale["fetchedAt"], old_fetched)
        self.assertEqual(stale["lastAttempt"], self.clock.value)
        self.assertNotIn("SECRET", json.dumps(stale, ensure_ascii=False))

        self.clock.value += 60
        self.write_credential(uid="other-fictional-user")
        switched = self.reader()
        switched.transport = FakeTransport(summary=OSError("offline"))
        result = switched()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["packages"], [])

    def test_auth_failure_clears_stale_cache(self):
        self.assertEqual(self.reader()()["status"], "known")
        self.clock.value += 60
        denied = self.reader()
        denied.transport = FakeTransport(summary=(401, b"SECRET server body"))
        result = denied()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
        self.assertFalse((self.cache / "buddy-quota.json").exists())
        later = self.reader()
        later.transport = FakeTransport(summary=OSError("offline"))
        self.assertEqual(later()["status"], "error")
        self.assertIsNone(later()["remaining"])

    def test_old_version_cache_with_suppressed_free_mon_is_not_stale(self):
        identity = hashlib.sha256(
            b"workbuddy\0" + UID.encode("utf-8") + b"\0" + API_DOMAIN.encode("ascii")
        ).hexdigest()
        self.cache.mkdir(parents=True, exist_ok=True)
        old = {
            "version": 2,
            "identity": identity,
            "data": {
                "plan": "标准版", "remaining": 500.0, "total": 500.0,
                "used": 0.0, "frozen": 0.0, "unit": "credit", "groups": [],
                "packages": [{
                    "name": "旧免费包", "remaining": 500.0, "total": 500.0,
                    "used": 0.0, "expireAt": None, "refreshAt": None,
                    "showExpireAt": True, "showRefreshAt": False,
                }],
                "packagesStatus": "known", "packagesError": None,
                "fetchedAt": self.clock.value - 10,
            },
        }
        (self.cache / "buddy-quota.json").write_text(
            json.dumps(old, ensure_ascii=False), encoding="utf-8")

        failing = self.reader()
        failing.transport = FakeTransport(summary=OSError("offline"))
        result = failing()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["packages"], [])
        self.assertNotIn("旧免费包", json.dumps(result, ensure_ascii=False))
        self.assertFalse((self.cache / "buddy-quota.json").exists())

    def test_redirect_and_error_shaped_success_are_rejected(self):
        redirect = self.reader()
        redirect.transport = FakeTransport(summary=(302, b""))
        self.assertEqual(redirect()["status"], "error")

        shaped = payload()
        shaped["code"] = 1001
        invalid = BuddyQuotaReader(self.cache, self.credential,
                                   transport=FakeTransport(summary=shaped),
                                   now=self.clock)
        self.assertEqual(invalid()["status"], "error")

        broken = BuddyQuotaReader(self.cache, self.credential,
                                  transport=FakeTransport(summary=(200, b"{")),
                                  now=self.clock)
        self.assertEqual(broken()["status"], "error")

    def test_package_expiry_refresh_flags_and_naive_beijing(self):
        summary = payload(total="160", remaining="110", used="50", frozen="0")
        template = summary["data"]["Packages"][0]
        summary["data"]["Packages"] = [dict(template, PackageCode=code,
            CycleTotalCapacity=total, CycleRemainCapacity=left, CycleUsedCapacity=used)
            for code, total, left, used in ((PRO_MON,"100","60","40"),
                (ACTIVITY,"50","40","10"),(FREE,"10","10","0"))]
        refresh = account(name="同名积分包", code=PRO_MON, remaining="60",
                          total="100", used="40", resource="r-refresh",
                          cycle_end="2026-11-01 09:59:59")
        expire = account(name="同名积分包", code=ACTIVITY, remaining="40",
                         total="50", used="10", resource="r-expire",
                         deduction_end=1793000000000,
                         cycle_end="2026-10-20 10:00:00")
        daily = account(name="免费积分", code=FREE, remaining="10", total="10",
                        used="0", resource="r-free",
                        cycle_end="2026-10-19 23:59:59")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([refresh, expire]),
                                       free=details_page([daily]))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["packagesStatus"], "known")
        self.assertEqual(len(result["packages"]), 3)

        by_remaining = {package["remaining"]: package for package in result["packages"]}
        refresh_package = by_remaining[60.0]
        expire_package = by_remaining[40.0]
        free_package = by_remaining[10.0]
        self.assertFalse(refresh_package["showExpireAt"])
        self.assertTrue(refresh_package["showRefreshAt"])
        self.assertEqual(refresh_package["refreshAt"],
                         beijing_timestamp("2026-11-01 09:59:59"))
        self.assertEqual(refresh_package["expireAt"],
                         beijing_timestamp("2026-11-01 09:59:59"))
        self.assertTrue(expire_package["showExpireAt"])
        self.assertFalse(expire_package["showRefreshAt"])
        self.assertEqual(expire_package["expireAt"], 1793000000)
        self.assertEqual(expire_package["refreshAt"],
                         beijing_timestamp("2026-10-20 10:00:00"))
        self.assertTrue(free_package["showExpireAt"])
        self.assertFalse(free_package["showRefreshAt"])
        self.assertEqual(free_package["expireAt"],
                         beijing_timestamp("2026-10-19 23:59:59"))
        self.assertIsNone(free_package["refreshAt"])
        self.assertEqual(sum(1 for package in result["packages"]
                             if package["name"] == "同名积分包"), 2)

        effective = []
        for package in result["packages"]:
            values = []
            if package["showExpireAt"] and package["expireAt"] is not None:
                values.append(package["expireAt"])
            if package["showRefreshAt"] and package["refreshAt"] is not None:
                values.append(package["refreshAt"])
            effective.append(min(values))
        self.assertEqual(effective, sorted(effective))

    def test_same_name_different_expiry_stays_separate(self):
        summary = payload(total="60", remaining="30", used="30", frozen="0")
        summary["data"]["Packages"][0]["PackageCode"] = ACTIVITY
        first = account(name="同名积分包", code=ACTIVITY, remaining="10",
                        total="20", used="10", resource="r-one",
                        deduction_end=1790000000000,
                        cycle_end="2026-10-20 10:00:00")
        second = account(name="同名积分包", code=ACTIVITY, remaining="20",
                         total="40", used="20", resource="r-two",
                         deduction_end=1793000000000,
                         cycle_end="2026-10-25 10:00:00")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([second, first]))
        result = self.reader()()
        packages = result["packages"]
        self.assertEqual(len(packages), 2)
        self.assertEqual([package["name"] for package in packages],
                         ["同名积分包", "同名积分包"])
        self.assertEqual([package["expireAt"] for package in packages],
                         [1790000000, 1793000000])

    def test_paged_details_32_and_capacity_type4(self):
        sliced = account(name="切片积分", remaining="6", total="100", used="999",
                         resource="slice", capacity_type=4,
                         slice=[{"SlicePeriodCapacitySizePrecise": "10",
                                 "SlicePeriodCapacityRemainPrecise": "6"}])
        cycle = account(name="周期积分", remaining="2", total="20", used="18",
                        resource="cycle", capacity_type=1)
        accounts = [dict(sliced, ResourceId="slice-%d" % i) for i in range(16)]
        accounts += [dict(cycle, ResourceId="cycle-%d" % i) for i in range(16)]
        summary = payload(total="1920", remaining="128", used="1792", frozen="0")

        def handler(url, data, headers, timeout):
            if url == API_URL:
                return 200, encode(summary)
            if url == PAID_URL:
                page = json.loads(data)["PageNumber"]
                if page == 1:
                    return 200, encode(details_page(accounts[:16], 32))
                if page == 2:
                    return 200, encode(details_page(accounts[16:], 32))
                return 200, encode(details_page([], 32))
            return 200, encode(details_page([]))

        self.transport = FakeTransport(handler=handler)
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["packagesStatus"], "known")
        self.assertEqual(len(result["packages"]), 32)
        sliced_package = [package for package in result["packages"]
                          if package["name"] == "切片积分"][0]
        self.assertEqual((sliced_package["remaining"], sliced_package["total"],
                          sliced_package["used"]), (6.0, 10.0, 4.0))
        paid_pages = [json.loads(call["data"])["PageNumber"]
                      for call in self.transport.calls if call["url"] == PAID_URL]
        self.assertEqual(paid_pages, [1, 2])

    def test_capacity_type4_without_slice_uses_cycle(self):
        summary = payload(total="30", remaining="10", used="20", frozen="0")
        package = account(name="切片缺失", remaining="10", total="30", used="20",
                          resource="no-slice", capacity_type=4, slice=[])
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([package]))
        result = self.reader()()
        self.assertEqual(result["packages"][0]["remaining"], 10.0)
        self.assertEqual(result["packages"][0]["total"], 30.0)
        self.assertEqual(result["packages"][0]["used"], 20.0)

    def test_overlapping_resource_dedup_and_conflict(self):
        summary = payload(total="20", remaining="10", used="10", frozen="0")
        shared = account(name="共享积分", remaining="10", total="20", used="10",
                         resource="same-resource")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([shared]),
                                       free=details_page([shared]))
        result = self.reader()()
        self.assertEqual(result["packagesStatus"], "known")
        self.assertEqual(len(result["packages"]), 1)

        conflict = dict(shared, CycleCapacityRemainPrecise="9")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([shared]),
                                       free=details_page([conflict]))
        conflicted = self.reader()()
        self.assertEqual(conflicted["status"], "known")
        self.assertEqual(conflicted["packagesStatus"], "error")
        self.assertEqual(conflicted["packages"], [])
        self.assertTrue(conflicted["packagesError"])

    def test_partial_details_failure_keeps_summary_known(self):
        self.transport = FakeTransport(summary=payload(),
                                       paid=(500, b"SECRET package body"))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["remaining"], 6450.5)
        self.assertEqual(result["packagesStatus"], "error")
        self.assertEqual(result["packages"], [])
        self.assertTrue(result["packagesError"])
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))

    def test_detail_mismatch_marks_packages_error(self):
        summary = payload(total="20", remaining="10", used="10", frozen="0")
        package = account(name="不一致积分", remaining="9", total="20", used="11",
                          resource="mismatch")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([package]))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["remaining"], 10.0)
        self.assertEqual(result["packagesStatus"], "error")
        self.assertEqual(result["packages"], [])
        self.assertTrue(result["packagesError"])

    def test_truncated_pagination_marks_unavailable(self):
        partial = account(name="截断积分", remaining="1", total="1", used="0",
                          resource="partial")

        def handler(url, data, headers, timeout):
            if url == API_URL:
                return 200, encode(payload())
            if url == PAID_URL:
                page = json.loads(data)["PageNumber"]
                if page == 1:
                    return 200, encode(details_page([partial], 33))
                return 200, encode(details_page([], 33))
            return 200, encode(details_page([]))

        self.transport = FakeTransport(handler=handler)
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["packagesStatus"], "error")
        self.assertEqual(result["packages"], [])
        self.assertIn("未完整", result["packagesError"])

    def test_missing_and_malformed_times_stay_unknown(self):
        summary = payload(total="5", remaining="5", used="0", frozen="0")
        package = account(name="无时间", remaining="5", total="5", used="0",
                          resource="no-time", cycle_end="not-a-date",
                          deduction_end="also-bad", expired="")
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([package]))
        result = self.reader()()
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["packagesStatus"], "known")
        self.assertIsNone(result["packages"][0]["expireAt"])
        self.assertIsNone(result["packages"][0]["refreshAt"])

    def test_identity_switch_during_transport_discards_snapshot(self):
        def handler(url, data, headers, timeout):
            if url == API_URL:
                self.write_credential(uid="other-fictional-user")
                return 200, encode(payload())
            if url == PAID_URL:
                return 200, encode(details_page([account()]))
            return 200, encode(details_page([]))

        self.transport = FakeTransport(handler=handler)
        result = self.reader()()
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["packages"], [])
        self.assertIn("切换", result["error"])
        self.assertFalse((self.cache / "buddy-quota.json").exists())

    def test_account_identifiers_and_secrets_are_not_serialized_or_cached(self):
        summary = payload()
        package = account(extra={
            "Uin": "SECRET-UIN",
            "AppId": "SECRET-APP",
            "AccountId": "SECRET-ACCOUNT",
            "DealName": "SECRET-DEAL",
            "AccountAttributes": "SECRET-ATTR",
            "Nickname": "SECRET-NICK",
        })
        package["ResourceId"] = "SECRET-RESOURCE"
        self.transport = FakeTransport(summary=summary,
                                       paid=details_page([package]))
        result = self.reader()()
        self.assertEqual(result["packagesStatus"], "known")
        serialized = json.dumps(result, ensure_ascii=False)
        cache_text = (self.cache / "buddy-quota.json").read_text(encoding="utf-8")
        for secret in (TOKEN, UID, "SECRET-UIN", "SECRET-APP", "SECRET-ACCOUNT",
                       "SECRET-DEAL", "SECRET-ATTR", "SECRET-NICK",
                       "SECRET-RESOURCE"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, cache_text)

    def test_service_quota_keeps_codex_and_buddy_independent(self):
        home = self.root / "codex"
        (home / "sessions").mkdir(parents=True)
        service = Service(self.root / "cache", home,
                          quota_reader=lambda _home: (_ for _ in ()).throw(
                              RuntimeError("Codex offline")),
                          buddy_quota_reader=self.reader())
        result = service.request("quota")
        buddy = result["quota"]["buddy"]
        self.assertEqual(buddy["status"], "known")
        self.assertEqual(buddy["remaining"], 6450.5)
        self.assertEqual(buddy["packagesStatus"], "known")
        self.assertTrue(result["quota"]["error"])
        self.assertEqual(result["quota"]["buckets"], [])
        self.assertEqual(service.request("status")["quota"]["buddy"]["status"], "known")

    def test_service_does_not_reuse_a_previous_buddy_snapshot_on_failure(self):
        home = self.root / "codex"
        (home / "sessions").mkdir(parents=True)
        first = Service(self.root / "cache", home,
                        quota_reader=lambda _home: (_ for _ in ()).throw(
                            RuntimeError("Codex offline")),
                        buddy_quota_reader=self.reader())
        self.assertEqual(first.request("quota")["quota"]["buddy"]["status"], "known")

        def broken_reader():
            return dict(status="error", plan=None, remaining=None, total=None,
                        used=None, frozen=None, unit="credit", groups=[],
                        packages=[], packagesStatus="error",
                        packagesError="WorkBuddy 登录状态无效，请在 WorkBuddy 中重新登录。",
                        fetchedAt=None, lastAttempt=int(time.time()),
                        source={"kind": "account", "label": "WorkBuddy 账号"},
                        error="WorkBuddy 登录状态无效，请在 WorkBuddy 中重新登录。")

        second = Service(self.root / "cache", home,
                         quota_reader=lambda _home: (_ for _ in ()).throw(
                             RuntimeError("Codex offline")),
                         buddy_quota_reader=broken_reader)
        buddy = second.request("quota")["quota"]["buddy"]
        self.assertEqual(buddy["status"], "error")
        self.assertIsNone(buddy["remaining"])

    def test_service_custom_home_keeps_buddy_reader_but_runtime_injection_skips_it(self):
        home = self.root / "codex"
        (home / "sessions").mkdir(parents=True)
        service = Service(self.root / "cache", home)
        self.assertIsInstance(service.buddy_quota_reader, BuddyQuotaReader)
        service.buddy_quota_reader.read = lambda: (_ for _ in ()).throw(
            AssertionError("default Buddy reader was used"))
        service.quota_reader = lambda _home: {"buckets": [], "fetchedAt": 1,
                                              "source": "account"}
        buddy = service.request("quota")["quota"]["buddy"]
        self.assertEqual(buddy["status"], "unknown")

        injected = Service(self.root / "cache3", None,
                           quota_reader=lambda _home: {"buckets": [], "fetchedAt": 1,
                                                       "source": "account"})
        self.assertIsNone(injected.buddy_quota_reader)


if __name__ == "__main__":
    unittest.main()
