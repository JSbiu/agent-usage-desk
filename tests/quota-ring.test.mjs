import test from 'node:test';
import assert from 'node:assert/strict';

import { buildQuotaRing, ringSegmentText } from '../extension/quota-ring.mjs';

const NOW = new Date(2026, 9, 19, 12, 0, 0);
const at = (year, month, day, hour = 12, minute = 0, second = 0) =>
  new Date(year, month - 1, day, hour, minute, second);
const epoch = (date) => Math.floor(date.getTime() / 1000);

function packageInfo(name, remaining, expireAt, options = {}) {
  return {
    name, remaining, total: remaining + 10, used: 10,
    expireAt: expireAt == null ? null : epoch(expireAt),
    refreshAt: options.refreshAt == null ? null : epoch(options.refreshAt),
    showExpireAt: options.showExpireAt ?? true,
    showRefreshAt: options.showRefreshAt ?? false,
  };
}

function buddy(packages, remaining, extra = {}) {
  return {
    status: 'known', plan: '标准版', remaining, total: remaining + 100, used: 100,
    frozen: 0, unit: 'credit', groups: [{ name: '套餐基础积分', remaining: 9999 }],
    packages, packagesStatus: 'known', packagesError: null,
    fetchedAt: epoch(NOW), lastAttempt: epoch(NOW),
    source: { kind: 'account', label: 'WorkBuddy 账号' }, error: null,
    ...extra,
  };
}

test('groups expiry days, later dates, refresh-only and unknown dates without double counting', () => {
  const packages = [
    packageInfo('今天', 100, at(2026, 10, 19, 23)),
    packageInfo('明天', 50, at(2026, 10, 20, 10)),
    packageInfo('后天', 25, at(2026, 10, 21, 10)),
    packageInfo('较晚', 10, at(2026, 10, 23, 10)),
    packageInfo('周期刷新', 20, at(2026, 10, 20, 10), {
      showExpireAt: false, showRefreshAt: true, refreshAt: at(2026, 10, 22, 10),
    }),
    packageInfo('时间未知', 10, null),
    packageInfo('已过期', 7, at(2026, 10, 19, 11, 59, 59)),
    packageInfo('零余额', 0, at(2026, 10, 20, 10)),
  ];
  const ring = buildQuotaRing(buddy(packages, 222), NOW);
  assert.equal(ring.state, 'ok');
  assert.equal(ring.total, 222);
  assert.equal(ring.packageTotal, 222);
  assert.equal(ring.segments.reduce((sum, segment) => sum + segment.value, 0), 222);
  assert.deepEqual(ring.segments.map((segment) => segment.label),
    ['已过期', '今天', '明天', '后天', '较晚到期', '周期刷新', '时间未知']);
  const byKind = Object.fromEntries(ring.segments.map((segment) => [segment.kind, segment]));
  const today = ring.segments.find((segment) => segment.label === '今天');
  assert.equal(byKind.overdue.value, 7);
  assert.equal(today.value, 100);
  assert.equal(byKind.later.value, 10);
  assert.equal(byKind.refresh.value, 20);
  assert.equal(byKind.refresh.count, 1);
  assert.equal(byKind.unknown.value, 10);
  assert.match(ringSegmentText(today), /今天 · 2026-10-19 · 100 credit · 1 个积分包/);
  assert.match(ringSegmentText(byKind.refresh), /周期刷新 .*按刷新周期，不是到期/);
});

test('refresh-only packages never become expiry-day segments', () => {
  const packages = [packageInfo('周期刷新', 20, at(2026, 10, 20, 10), {
    showExpireAt: false, showRefreshAt: true, refreshAt: at(2026, 10, 22, 10),
  })];
  const ring = buildQuotaRing(buddy(packages, 20), NOW);
  assert.equal(ring.segments.length, 1);
  assert.equal(ring.segments[0].kind, 'refresh');
  assert.equal(ring.segments[0].value, 20);
  assert.equal(ring.segments[0].date, undefined);
});

test('uncovered summary balance is an explicit residual, not a guessed expiry', () => {
  const packages = [packageInfo('今天', 100, at(2026, 10, 19, 23))];
  const ring = buildQuotaRing(buddy(packages, 300), NOW);
  assert.equal(ring.state, 'ok');
  assert.equal(ring.residual, 200);
  const residual = ring.segments.find((segment) => segment.kind === 'residual');
  assert.equal(residual.value, 200);
  assert.equal(residual.label, '其他余额');
  assert.equal(ring.segments.reduce((sum, segment) => sum + segment.value, 0), 300);
});

test('package totals above the summary are refused instead of silently scaled', () => {
  const packages = [packageInfo('超量', 150, at(2026, 10, 19, 23))];
  const ring = buildQuotaRing(buddy(packages, 100), NOW);
  assert.equal(ring.state, 'mismatch');
  assert.deepEqual(ring.segments, []);
});

test('zero known differs from unknown and package-detail failure', () => {
  assert.equal(buildQuotaRing(buddy([], 0), NOW).state, 'zero');
  assert.equal(buildQuotaRing(buddy([], 0, { status: 'unknown' }), NOW).state, 'unavailable');
  assert.equal(buildQuotaRing(buddy([], 100, { packagesStatus: 'error' }), NOW).state, 'unavailable');
  assert.equal(buildQuotaRing({ ...buddy([], 100), remaining: null }, NOW).state, 'unavailable');
});
