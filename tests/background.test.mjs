import test from 'node:test';
import assert from 'node:assert/strict';
import { Coordinator, ALARM, QUOTA_ALARM, INTERVAL, QUOTA_INTERVAL, REFRESH_MINUTES } from '../extension/coordinator.mjs';
import { tokens, percent } from '../extension/format.mjs';
import { windowLabel, quotaPercent, resetCountdown } from '../extension/quota.mjs';

function mock(last = 10000) {
  let now = last * 1000 + 1000, creates = 0, calls = 0;
  const alarms = new Map([[ALARM, { name: ALARM, periodInMinutes: 1, scheduledTime: now + INTERVAL }], [QUOTA_ALARM, { name: QUOTA_ALARM, periodInMinutes: 5, scheduledTime: now + QUOTA_INTERVAL }]]);
  const values = {};
  const api = { storage: { local: { get: async (key) => ({ [key]: values[key] }), set: async (data) => Object.assign(values, structuredClone(data)) } }, alarms: { get: async (name) => alarms.get(name), create: async (name, options) => { creates++; alarms.set(name, { name, periodInMinutes: options.periodInMinutes, scheduledTime: now + options.delayInMinutes * 60000 }); } } };
  let finish;
  const native = async (op) => {
    if (op === 'status') return { lastSuccess: last, version: 1 };
    if (op === 'refresh') { calls++; return new Promise((resolve, reject) => { finish = (failed = false) => failed ? reject(new Error('读取失败')) : resolve({ status: { lastSuccess: last + 10, version: 2 }, import: { partial: false } }); }); }
  };
  return { api, native, values, alarms, now: () => now, advance: (ms) => { now += ms; }, finish: (...args) => finish(...args), get calls() { return calls; }, get creates() { return creates; }, removeAlarm: (name = ALARM) => alarms.delete(name) };
}

test('manual and automatic refresh coalesce and keep scheduled time', async () => {
  const m = mock(), c = new Coordinator(m.api, m.native, m.now);
  await c.initialize(); const scheduled = c.state.nextAlarm;
  const a = c.refresh(); const b = c.alarm({ name: ALARM });
  await new Promise((r) => setImmediate(r)); assert.equal(m.calls, 1);
  m.finish(); await Promise.all([a, b]);
  assert.equal(c.state.nextAlarm, scheduled); assert.equal(m.creates, 0); assert.equal(c.state.busy, false);
});

test('worker restart restores missing alarm and catches up once', async () => {
  const m = mock(); m.removeAlarm();
  const c = new Coordinator(m.api, m.native, () => m.now() + INTERVAL);
  await Promise.all([c.initialize(), c.initialize()]);
  await new Promise((r) => setImmediate(r));
  assert.equal(m.creates, 1); assert.equal(m.calls, 1);
  m.finish(); await c.running;
});

test('upgrade backfills turn timing even when the latest import is recent', async () => {
  const m = mock();
  const native = async (op, params) => op === 'status'
    ? { ...await m.native(op, params), responseBackfillPending: true } : m.native(op, params);
  const c = new Coordinator(m.api, native, m.now);
  await c.initialize();
  const scheduled = c.state.nextAlarm;
  await new Promise((r) => setImmediate(r));
  assert.equal(m.calls, 1);
  m.finish(); await c.running;
  assert.equal(c.state.nextAlarm, scheduled);
});

test('upgrade backfills Buddy first-token evidence after a recent import', async () => {
  const m = mock();
  const native = async (op, params) => op === 'status'
    ? { ...await m.native(op, params), buddy: { responseBackfillPending: true } } : m.native(op, params);
  const c = new Coordinator(m.api, native, m.now);
  await c.initialize();
  await new Promise((r) => setImmediate(r));
  assert.equal(m.calls, 1);
  m.finish(); await c.running;
});

test('failed refresh preserves last successful data', async () => {
  const m = mock(), c = new Coordinator(m.api, m.native, m.now);
  await c.initialize(); const a = c.refresh();
  await new Promise((r) => setImmediate(r)); m.finish(true);
  await assert.rejects(a, /读取失败/); assert.equal(c.state.status.version, 1); assert.equal(c.state.busy, false);
});

test('token units and cache percentages match display contract', () => {
  assert.equal(tokens(64528372), '64.53M'); assert.equal(tokens(999999), '1M');
  assert.equal(percent(.9401), '94.0%'); assert.equal(percent(null), '—');
});

test('account wait is independent of data refresh and preserves prior quota', async () => {
  const m = mock(); let finishQuota, quotaCalls = 0;
  const previous = { fetchedAt: 10000, lastAttempt: 10000, buckets: [{ windows: [{ used: 70 }] }] };
  const native = (op, params) => {
    if (op === 'status') return Promise.resolve({ home: 'home', version: 1, lastSuccess: 10000, quota: previous });
    if (op === 'quota') { quotaCalls++; return new Promise((resolve) => { finishQuota = resolve; }); }
    if (op === 'refresh') return m.native(op, params).then((result) => ({ ...result, status: { ...result.status, home: 'home', quota: previous } }));
    return m.native(op, params);
  };
  const c = new Coordinator(m.api, native, m.now);
  await c.initialize();
  const waiting = c.refreshQuota(), refreshing = c.refresh();
  await new Promise((r) => setImmediate(r));
  assert.equal(quotaCalls, 1); assert.equal(c.state.quota.buckets[0].windows[0].used, 70);
  m.finish(); await refreshing;
  assert.equal(c.state.busy, false); assert.equal(c.state.quotaBusy, true);
  assert.equal(c.state.quota.buckets[0].windows[0].used, 70);
  finishQuota({ home: 'home', quota: { ...previous, lastAttempt: 10001, buckets: [{ windows: [{ used: 72 }] }] } });
  await waiting;
  assert.equal(c.state.quotaBusy, false); assert.equal(c.state.quota.buckets[0].windows[0].used, 72);
});

test('same query coalesces, reuses unchanged version, and reloads a changed version', async () => {
  const m = mock(); let version = 1, reads = 0;
  const native = async (op) => {
    const status = { home: 'home', version, lastSuccess: 10000, quota: { fetchedAt: 10000, lastAttempt: 10000 } };
    if (op === 'status') return status;
    if (op === 'refresh') return { status, import: { partial: false } };
    if (op === 'details') { reads++; return { status, rows: [], total: 0 }; }
    if (op === 'quota') return { home: 'home', quota: status.quota };
  };
  const c = new Coordinator(m.api, native, m.now);
  const message = { op: 'details', params: { model: 'gpt-6.1-sol' } };
  await Promise.all([c.dispatch(message), c.dispatch(message)]);
  assert.equal(reads, 1);
  await c.refresh(); await c.dispatch(message); assert.equal(reads, 1);
  version = 2; await c.refresh(); await c.dispatch(message); assert.equal(reads, 2);
});

test('quota windows follow server duration and missing or expired values stay explicit', () => {
  assert.equal(windowLabel(300, 'secondary'), '5 小时额度');
  assert.equal(windowLabel(10080, 'primary'), '每周额度');
  assert.equal(windowLabel(60, 'primary'), '1 小时额度');
  assert.equal(quotaPercent(null), '—'); assert.equal(quotaPercent(27.5), '27.5%');
  assert.equal(resetCountdown(10, 11000), '已到重置时间，等待额度更新');
});

test('hourly upgrade migrates both alarms; every interval persists without shifting manual schedule', async () => {
  const m = mock();
  m.alarms.get(ALARM).periodInMinutes = 60; m.removeAlarm(QUOTA_ALARM);
  m.values.monitorState = { status: { home: 'home', version: 1 }, busy: true };
  let configureCalls = 0;
  const native = async (op) => {
    const status = { home: 'home', version: 1, lastSuccess: m.now() / 1000 };
    if (op === 'status') return status;
    if (op === 'refresh') return { status, import: { partial: false } };
    if (op === 'configure') configureCalls++;
  };
  const c = new Coordinator(m.api, native, m.now);
  await c.initialize();
  assert.equal(m.creates, 2); assert.equal(c.state.refreshMinutes, 1);
  for (const minutes of REFRESH_MINUTES) {
    await c.dispatch({ op: 'configure', params: { home: 'home', refreshMinutes: minutes } });
    assert.equal(m.alarms.get(ALARM).periodInMinutes, minutes);
    assert.equal(m.values.monitorState.refreshMinutes, minutes);
    assert.equal(m.alarms.get(QUOTA_ALARM).periodInMinutes, 5);
    const scheduled = c.state.nextAlarm;
    await c.dispatch({ op: 'refresh' }); assert.equal(c.state.nextAlarm, scheduled);
  }
  assert.equal(configureCalls, 0);
  const restarted = new Coordinator(m.api, native, m.now);
  await restarted.initialize(); assert.equal(restarted.state.refreshMinutes, 60);
  await assert.rejects(c.dispatch({ op: 'configure', params: { home: 'home', refreshMinutes: 2 } }), /支持/);
});

test('local alarms and page visits keep quota on its own interval; manual refresh forces quota', async () => {
  const m = mock(); let localCalls = 0, quotaCalls = 0;
  const native = async (op) => {
    const status = { home: 'home', version: 1, lastSuccess: m.now() / 1000 };
    if (op === 'status') return status;
    if (op === 'refresh') { localCalls++; return { status, import: { partial: false } }; }
    if (op === 'quota') { quotaCalls++; return { home: 'home', quota: { lastAttempt: m.now() / 1000 } }; }
  };
  const c = new Coordinator(m.api, native, m.now);
  await c.initialize(); await c.quotaRunning?.promise;
  await Promise.all([c.dispatch({ op: 'visit' }), c.dispatch({ op: 'visit' }), c.alarm({ name: ALARM })]);
  assert.equal(localCalls, 1); assert.equal(quotaCalls, 1);
  for (let i = 0; i < 5; i++) { m.advance(INTERVAL); await c.alarm({ name: ALARM }); }
  assert.equal(quotaCalls, 1);
  await c.alarm({ name: QUOTA_ALARM }); await c.quotaRunning?.promise;
  assert.equal(quotaCalls, 2);
  m.advance(4 * INTERVAL);
  await c.dispatch({ op: 'refresh' }); await c.quotaRunning?.promise;
  assert.equal(quotaCalls, 3);
  m.advance(INTERVAL);
  await c.alarm({ name: QUOTA_ALARM });
  assert.equal(quotaCalls, 4);
});

test('failed quota attempts keep the last snapshot and throttle retries across worker restarts', async () => {
  const m = mock(); let quotaCalls = 0;
  const previous = { lastAttempt: 9000, fetchedAt: 9000, buckets: [{ windows: [{ used: 70 }] }] };
  const native = async (op) => {
    const status = { home: 'home', version: 1, lastSuccess: m.now() / 1000, quota: previous };
    if (op === 'status') return status;
    if (op === 'refresh') return { status, import: { partial: false } };
    if (op === 'quota') { quotaCalls++; throw new Error('账号网络失败'); }
  };
  let c = new Coordinator(m.api, native, m.now);
  await c.initialize(); await c.quotaRunning?.promise.catch(() => {});
  assert.equal(quotaCalls, 1); assert.equal(c.state.quota.buckets[0].windows[0].used, 70);
  c = new Coordinator(m.api, native, m.now);
  await c.initialize(); await c.dispatch({ op: 'visit' });
  assert.equal(quotaCalls, 1);
  m.advance(QUOTA_INTERVAL); await c.alarm({ name: QUOTA_ALARM }).catch(() => {});
  assert.equal(quotaCalls, 2); assert.equal(c.state.quota.buckets[0].windows[0].used, 70);
});
