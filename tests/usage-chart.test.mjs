import test from 'node:test';
import assert from 'node:assert/strict';

import {
  aggregateUsage, formatUsageAxis, formatUsageValue, modelColor, niceUsageMax,
} from '../extension/usage-chart.mjs';

const rows = [
  { day: '2026-10-18', source: 'codex', model: 'alpha', records: 2, input: 100, output: 60, cost: .01, priced: 2 },
  { day: '2026-10-18', source: 'workbuddy-desktop', model: 'alpha', records: 1, input: 20, output: 10, cost: null, priced: 0 },
  { day: '2026-10-18', source: 'codebuddy-cli', model: 'beta', records: 1, input: 30, output: 5, cost: 0, priced: 1 },
  { day: '2026-10-19', source: 'codex', model: 'alpha', records: 1, input: 50, output: 40, cost: .02, priced: 1 },
  { day: '2026-10-19', source: 'workbuddy-desktop', model: 'gamma', records: 2, input: 10, output: 3, cost: null, priced: 0 },
];

test('same model is combined across tools into one daily segment', () => {
  const data = aggregateUsage(rows, { metric: 'output' });
  assert.deepEqual(data.days.map((day) => day.day), ['2026-10-18', '2026-10-19']);
  const alpha = data.days[0].segments.find((segment) => segment.model === 'alpha');
  assert.equal(alpha.value, 70);
  assert.equal(alpha.records, 3);
  assert.equal(data.days[0].total, 75);
  assert.equal(data.days[1].total, 43);
  assert.deepEqual(data.models, ['alpha', 'beta', 'gamma']);
});

test('cost keeps known zero separate from unknown and reports partial counts', () => {
  const data = aggregateUsage(rows, { metric: 'cost', currency: 'USD' });
  const first = data.days[0];
  assert.equal(first.total, .01);
  assert.equal(first.priced, 3);
  assert.equal(first.unknownRecords, 1);
  assert.equal(first.partial, true);
  const alpha = first.segments.find((segment) => segment.model === 'alpha');
  const beta = first.segments.find((segment) => segment.model === 'beta');
  assert.equal(alpha.value, .01);
  assert.equal(alpha.unknownRecords, 1);
  assert.equal(beta.value, 0);
  assert.equal(beta.costKnown, true);
  assert.equal(beta.priced, 1);
  const gamma = data.days[1].segments.find((segment) => segment.model === 'gamma');
  assert.equal(gamma.value, 0);
  assert.equal(gamma.costKnown, false);
  assert.equal(gamma.unknownRecords, 2);
  assert.equal(data.unknownRecords, 3);
});

test('unknown-only cost never becomes a free value', () => {
  const data = aggregateUsage([
    { day: '2026-10-20', source: 'workbuddy-desktop', model: 'delta', records: 2, cost: null, priced: 0 },
  ], { metric: 'cost' });
  assert.equal(data.total, 0);
  assert.equal(data.priced, 0);
  assert.equal(data.unknownRecords, 2);
  assert.equal(data.days[0].allUnknown, true);
  assert.equal(data.days[0].segments[0].costKnown, false);
});

test('currency conversion uses the fixed 6.8 FX rate', () => {
  assert.equal(formatUsageValue(.01, 'cost', 'USD'), '~$0.0100');
  assert.equal(formatUsageValue(.01, 'cost', 'CNY'), '~¥0.0680');
  assert.equal(formatUsageAxis(.01, 'cost', 'CNY'), '~¥0.068');
  assert.equal(formatUsageValue(1234, 'output', 'USD'), '1,234 token');
});

test('axis maxima and model colors are stable', () => {
  assert.ok(niceUsageMax(350) >= 350);
  assert.ok(niceUsageMax(.02, 'cost') >= .02);
  const models = ['alpha', 'beta'];
  assert.equal(modelColor('alpha', models), modelColor('alpha', models));
  assert.notEqual(modelColor('alpha', models), modelColor('beta', models));
});
