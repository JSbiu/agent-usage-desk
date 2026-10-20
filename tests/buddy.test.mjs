import test from 'node:test';
import assert from 'node:assert/strict';
import { Coordinator, ALARM, QUOTA_ALARM } from '../extension/coordinator.mjs';
import { sourceLabel, sessionLabel, credit, money } from '../extension/format.mjs';

test('cost uses fixed USD/CNY conversion and preserves unknown and zero', () => {
  assert.equal(money(1), '~$1.0000');
  assert.equal(money(1, 'CNY'), '~¥6.8000');
  assert.equal(money(0, 'CNY'), '~¥0.0000');
  assert.equal(money(null, 'CNY'), '—');
  assert.equal(money(6.8 / 6.8, 'CNY'), '~¥6.8000');
});

function mock(status) {
  const now = 10000 * 1000;
  const alarms = new Map([
    [ALARM, { name: ALARM, periodInMinutes: 1, scheduledTime: now + 60000 }],
    [QUOTA_ALARM, { name: QUOTA_ALARM, periodInMinutes: 5, scheduledTime: now + 300000 }],
  ]);
  const values = {};
  const api = {
    storage: { local: {
      get: async (key) => ({ [key]: values[key] }),
      set: async (data) => Object.assign(values, structuredClone(data)),
    } },
    alarms: {
      get: async (name) => alarms.get(name),
      create: async (name, options) => alarms.set(name, {
        name, periodInMinutes: options.periodInMinutes,
        scheduledTime: now + options.delayInMinutes * 60000,
      }),
    },
  };
  const calls = { configure: [], details: 0 };
  const native = async (op, params) => {
    if (op === 'status') return structuredClone(status);
    if (op === 'refresh') return { status: structuredClone(status), import: { partial: false } };
    if (op === 'quota') return { home: status.home, quota: { lastAttempt: now / 1000 } };
    if (op === 'configure') { calls.configure.push(params); return {}; }
    if (op === 'details') { calls.details++; return { status: structuredClone(status), rows: [], total: 0 }; }
    return {};
  };
  return { api, native, calls, values, now: () => now };
}

const base = {
  home: 'home', version: 1, lastSuccess: 10000,
  buddy: { roots: { workbuddy: 'wb', codebuddy: 'cb', bridge: 'br' } },
};

test('source, session and credit labels keep unknown values explicit', () => {
  assert.equal(sourceLabel('codex'), 'Codex');
  assert.equal(sourceLabel('workbuddy-desktop'), 'WorkBuddy 桌面端');
  assert.equal(sourceLabel('codebuddy-cli'), 'CodeBuddy CLI');
  assert.equal(sourceLabel('all'), '全部来源');
  assert.equal(sourceLabel(''), '—');
  assert.equal(credit(0), '0');
  assert.equal(credit(null), '—');
  assert.equal(sessionLabel(null), '—');
  assert.equal(sessionLabel({ outcome: 'resumed', runCount: 3 }), '续接已证实 · 3 次运行');
  assert.equal(sessionLabel({ outcome: 'new', runCount: 1 }), '新会话 · 1 次运行');
  assert.equal(sessionLabel({ outcome: 'mismatch', runCount: 2 }), '续接不匹配 · 2 次运行');
  assert.equal(sessionLabel({ outcome: 'unknown', runCount: 0 }), '未知');
  assert.equal(sessionLabel({ outcome: 'failed', runCount: 3, confirmedResumeCount: 1, failedCount: 1 }),
    '续接已证实 · 3 次运行 · 含异常/未证实');
  assert.equal(sessionLabel({ runCount: 2, confirmedResumeCount: 1, unconfirmedCount: 1 }),
    '续接已证实 · 2 次运行 · 含异常/未证实');
  assert.equal(sessionLabel({ runCount: 2, confirmedResumeCount: 1, pendingCount: 1 }),
    '续接已证实 · 2 次运行 · 含执行中');
  assert.equal(sessionLabel({ outcome: 'failed', runCount: 2, matchedResumeCount: 1 }),
    '续接编号一致 · 2 次运行 · 未成功完成');
});

test('buddy directory changes call configure while unchanged paths do not', async () => {
  const m = mock(base);
  const c = new Coordinator(m.api, m.native, m.now);
  await c.initialize();
  await c.dispatch({ op: 'configure', params: {
    home: 'home', refreshMinutes: 1, workbuddy: 'wb', codebuddy: 'cb', bridge: 'br',
  } });
  assert.deepEqual(m.calls.configure, []);
  await c.dispatch({ op: 'configure', params: {
    home: 'home', refreshMinutes: 1, workbuddy: 'wb2', codebuddy: 'cb', bridge: 'br2',
  } });
  assert.deepEqual(m.calls.configure, [{ workbuddy: 'wb2', bridge: 'br2' }]);
  await c.dispatch({ op: 'configure', params: {
    home: 'home', refreshMinutes: 1, workbuddy: 'wb2', codebuddy: '', bridge: 'br2',
  } });
  assert.deepEqual(m.calls.configure.at(-1), { workbuddy: 'wb2', codebuddy: '', bridge: 'br2' });
});

test('buddy-only configure does not require resending the Codex home', async () => {
  const m = mock(base);
  const c = new Coordinator(m.api, m.native, m.now);
  await c.initialize();
  await c.dispatch({ op: 'configure', params: { refreshMinutes: 1, workbuddy: 'new-workbuddy' } });
  assert.deepEqual(m.calls.configure, [{ workbuddy: 'new-workbuddy' }]);
});

test('changed buddy roots invalidate cached queries even at the same version', async () => {
  const m = mock(base);
  let current = structuredClone(base);
  const native = async (op, params) => {
    if (op === 'status') return structuredClone(current);
    if (op === 'refresh') return { status: structuredClone(current), import: { partial: false } };
    if (op === 'quota') return { home: current.home, quota: { lastAttempt: m.now() / 1000 } };
    if (op === 'configure') {
      m.calls.configure.push(params);
      current = { ...current, buddy: { roots: { ...current.buddy.roots, workbuddy: params.workbuddy } } };
      return {};
    }
    if (op === 'details') { m.calls.details++; return { status: structuredClone(current), rows: [], total: 0 }; }
    return {};
  };
  const c = new Coordinator(m.api, native, m.now);
  await c.initialize();
  const message = { op: 'details', params: {} };
  await c.dispatch(message);
  await c.dispatch(message);
  assert.equal(m.calls.details, 1);
  await c.dispatch({ op: 'configure', params: { refreshMinutes: 1, workbuddy: 'elsewhere' } });
  await c.dispatch(message);
  assert.equal(m.calls.details, 2);
});
