import { DASH, tokens, full, percent, rate, seconds, money, credit, range, time, sourceLabel, sessionLabel } from './format.mjs';
import { renderQuota } from './quota.mjs';
import { renderUsageChart } from './usage-chart.mjs';

const $ = (selector) => document.querySelector(selector);
const prefs = { tab: 'details', preset: '3', currency: 'USD', pageSize: 50, sort: 'time', direction: 'desc', statsDirection: 'desc', metric: 'average', usageMetric: 'output' };
const costText = (value) => money(value, prefs.currency);
let state = {}, page = 1, snapshot = null, statsRows = [], commonRows = [], commonCards = null;
let commonCache = null, tableScope = null, sequence = 0, commonRequest = 0, chartDrawn = false;
let activeLoad = null;
let booted = false;
const hiddenSeries = new Set();

async function send(op, params = {}) {
  const response = await chrome.runtime.sendMessage({ op, params });
  if (!response?.ok) throw new Error(response?.error || '扩展后台未响应，请重新打开页面。');
  return response.data;
}

const savePrefs = () => chrome.storage.local.set({ preferences: prefs });

function dateRange() {
  if (prefs.preset === 'custom') return { from: $('#from').value, to: $('#to').value };
  return range(Number(prefs.preset));
}

function filters(scope = null) {
  const dates = scope ? { from: scope.day, to: scope.day } : dateRange();
  return { ...dates, source: scope?.source ?? 'all', model: scope?.model ?? '', mode: '', mergeModes: true };
}

function commonKey() {
  return JSON.stringify([dateRange(), state.status?.home, state.status?.buddy?.roots ?? null]);
}

function controls() {
  $('#preset').value = prefs.preset;
  $('#page-size').value = String(prefs.pageSize);
  $('#metric').value = prefs.metric;
  $('#usage-metric').value = prefs.usageMetric;
  const costOption = $('#usage-metric option[value="cost"]');
  if (costOption) costOption.textContent = `成本 · ${prefs.currency}`;
  $('#custom-dates').hidden = prefs.preset !== 'custom';
  for (const tab of document.querySelectorAll('[data-tab]')) {
    const active = tab.dataset.tab === prefs.tab;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-pressed', String(active));
  }
  const scoped = prefs.tab === 'details' && !!tableScope;
  $('#table-scope').hidden = !scoped;
  if (scoped) {
    $('#table-scope-label').textContent = `表格聚焦 ${tableScope.day} · ${sourceLabel(tableScope.source)} · ${tableScope.model}`;
  }
  $('#table-title').textContent = prefs.tab === 'stats' ? '每日统计' : '请求明细';
  $('#table-subtitle').textContent = prefs.tab === 'stats'
    ? '日期 × 工具 × 模型 · 点击一行聚焦表格明细'
    : scoped ? '仅表格聚焦 · 日期、卡片与图表仍为全部范围'
      : '全部工具与模型 · 按回复完成时间展示';
  $('#data-table').classList.toggle('stats', prefs.tab === 'stats');
  const { from, to } = dateRange();
  $('#range-note').textContent = from + ' — ' + to;
}

function showError(error) {
  $('#error').hidden = !error;
  $('#error span').textContent = error || '';
}

function status(next) {
  state = next || {};
  const s = state.status;
  const busy = !!state.busy && (!snapshot || state.reason === '手动刷新');
  $('#refresh').disabled = busy;
  $('#refresh').classList.toggle('busy', busy);
  $('#refresh span').textContent = busy ? '正在刷新' : '刷新数据';
  const progress = state.progress;
  const interval = state.refreshMinutes === 0.5 ? '30 秒' : `${state.refreshMinutes || 1} 分钟`;
  $('#refresh-state').textContent = state.busy ? (progress ? `读取日志 ${progress.completed}/${progress.total}` : '正在更新本地索引…') : state.error ? '连接或刷新异常' : s ? `每 ${interval}自动刷新` : '正在连接…';
  $('#refresh-note').textContent = `记录每 ${interval}自动刷新；账号额度每 5 分钟更新。`;
  const buddySources = Object.values(s?.buddy?.sources || {});
  const latest = Math.max(s?.latest || 0, ...buddySources.map((item) => item.latest || 0), 0);
  const successes = [s?.lastSuccess, s?.buddy?.enabled ? s?.buddy?.lastSuccess : s?.lastSuccess]
    .filter((value) => value != null);
  const success = successes.length ? Math.min(...successes) : null;
  $('#last-success').textContent = time(success, true);
  $('#latest').textContent = time(latest, true);
  $('#next-alarm').textContent = time(state.nextAlarm ? state.nextAlarm / 1000 : null, true);
  const last = s?.lastImport, buddyLast = s?.buddy?.lastImport;
  $('#import-feedback').textContent = last || buddyLast
    ? `上次新增：Codex ${full(last?.added || 0)} 条 · 其他来源 ${full(buddyLast?.added || 0)} 条`
    : '';
  // 只有“曾经有记录、现在目录不可用”才算异常；从未用过的来源只是没有数据，
  // 在设置里标注状态即可，不必用警告条催用户去配目录。
  const missing = (s?.sources || [])
    .filter((item) => !item.available && (item.records || 0) > 0)
    .map((item) => `未发现 ${item.label} 来源，请检查设置目录。`);
  showError(state.error || s?.buddy?.warning || s?.buddy?.warnings?.join('；') || missing.join('；'));
  renderQuota(state.quota ?? s?.quota, !!state.quotaBusy);
}

function cards(c) {
  c = c || {};
  $('#card-records').textContent = full(c.records);
  $('#card-timed').textContent = `${full(c.timed)} 条可测速 / ${full(c.records)} 条记录`;
  $('#card-cost').textContent = costText(c.cost);
  const codexRecords = c.codexRecords ?? c.records;
  const buddyRecords = Math.max(0, c.records - codexRecords);
  $('#card-priced').textContent = c.records && c.priced < c.records
    ? `已计价 ${full(c.priced)}/${full(c.records)} 条` + (c.priced ? ' · 部分' : ' · 价格或缓存未知')
    : `官方参考价 · ${state.status?.pricingChecked || '2026-10-19'}`;
  $('#card-credit').hidden = !c.creditApplicable;
  $('#card-credit').textContent = creditNote(c);
  $('#card-cache').textContent = percent(c.cacheHit);
  $('#card-cache-note').textContent = c.cacheHit == null && c.records && c.cacheKnown < c.records
    ? '部分记录缓存未知，未计入' : '缓存输入 / 总输入';
  $('#card-speed').textContent = rate(c.average);
  $('#card-speed-note').textContent = buddyRecords
    ? `生成追踪 ${full(c.traceTimed || 0)} · 边界估算 ${full(c.boundaryTimed || 0)}` : '包含等待与推理 token';
}

function td(value, classes = '', title) {
  const cell = document.createElement('td');
  cell.textContent = value;
  cell.className = classes;
  if (title) cell.title = title;
  return cell;
}

function creditNote(r, daily = true) {
  const valid = daily ? r.creditCount : r.creditValid;
  return `credit ${valid ? credit(r.credit) : DASH}` + (daily && r.creditApplicable
    ? ` · ${full(r.creditCount)}/${full(r.creditApplicable)} 条` : '');
}

function costCell(r, daily = false) {
  const value = r.cost, partial = daily && r.priced < r.records;
  const cell = td('', 'mono numeric cost-cell', '官方 API 价格快照，供参考；日志 credit 为原值，不参与货币换算。' + (partial ? '\n仅合计已知价格的记录。' : ''));
  const main = document.createElement('span'); main.textContent = costText(value); cell.append(main);
  if (partial && value != null) {
    const label = document.createElement('span');
    label.className = 'speed-unit';
    label.textContent = '部分';
    cell.append(label);
  }
  if (daily ? r.creditApplicable > 0 : r.sourceId !== 'codex') {
    const note = document.createElement('small'); note.className = 'response-secondary credit-note';
    note.textContent = creditNote(r, daily); cell.append(note);
  }
  return cell;
}

function sourceCell(r) {
  const value = r.sourceId || r.source;
  return td(sourceLabel(value), 'source-cell', sourceLabel(value));
}

function sessionCell(r) {
  const cell = td(sessionLabel(r.session), 'session-cell');
  if (r.session) {
    const lines = [
      `实际返回编号：${r.session.actualSessionId || DASH}`,
      `关联运行：${full(r.session.runCount)} 次`,
      `已成功匹配续接：${full(r.session.confirmedResumeCount)} 次`,
    ];
    if (r.session.mismatchCount) lines.push(`不匹配：${full(r.session.mismatchCount)} 次`);
    if (r.session.failedCount) lines.push(`失败：${full(r.session.failedCount)} 次`);
    if (r.session.unconfirmedCount) lines.push(`未证实：${full(r.session.unconfirmedCount)} 次`);
    if (r.session.pendingCount) lines.push(`执行中：${full(r.session.pendingCount)} 次`);
    if (r.session.contextCompression == null) lines.push('上下文压缩：未知（同一会话不代表未压缩）');
    cell.title = lines.join('\n');
  }
  return cell;
}

const responseDefinition = '首 token 等待。Codex 每轮仅首个请求计一次，轮次结束后可用；WorkBuddy/CLI 按每次模型请求记录，可能包含思考或工具片段。缺失不计为零。';
const FIRST_TOKEN_SOURCE = { 'completed-log': '完成日志', 'first-token-log': '首字诊断日志', request: '按请求记录' };
const firstTokenSourceLabel = (value) => FIRST_TOKEN_SOURCE[value] || '来源未标注';
function responseDefinitionFor(r) {
  const source = r.sourceId || r.source;
  if (source === 'codex') return 'Codex 首 token：每轮仅在首个请求记录一次，轮次结束后可用。缺失不计为零。';
  return 'WorkBuddy/CLI 首 token：按每次模型请求记录，可能包含思考或工具片段，可能早于可见正文。与 Codex 每轮仅首个请求计一次不同；缺失不计为零。';
}
function responseCell(r, daily = false) {
  const value = daily ? r.responseMedian : r.firstToken;
  const cell = td('', 'mono numeric response-cell speed-cell');
  const definition = responseDefinitionFor(r);
  cell.title = definition;
  const main = document.createElement('span'); main.className = 'response-value';
  if (daily && value != null) {
    const label = document.createElement('span'); label.className = 'response-label'; label.textContent = '中位'; main.append(label);
  }
  main.append(document.createTextNode(seconds(value)));
  if (value != null) { const unit = document.createElement('span'); unit.className = 'speed-unit'; unit.textContent = 's'; main.append(unit); }
  cell.append(main);
  if (daily && r.responseTurns) {
    const source = r.sourceId || r.source;
    const unit = source === 'codex' ? '轮' : '次请求';
    const associated = source === 'codex' ? '可关联的已结束轮次' : '可关联请求';
    const note = document.createElement('small'); note.className = 'response-secondary';
    note.textContent = (r.responseP90 == null ? '' : `P90 ${seconds(r.responseP90)} s · `) + `${full(r.responseSamples)}/${full(r.responseTurns)} ${unit}`;
    cell.append(note);
    cell.title += `\n中位数 ${value == null ? DASH : value.toFixed(3) + ' 秒'}；P90 ${r.responseP90 == null ? DASH : r.responseP90.toFixed(3) + ' 秒'}。\n有效样本 / ${associated}：${r.responseSamples}/${r.responseTurns}。P90 使用最近秩法。`;
  } else if (!daily && value != null) cell.title += `\n${sourceLabel(r.sourceId)} 首 token 等待 ${value.toFixed(3)} 秒。`;
  else if (!daily && r.firstInTurn) cell.title += '\n本轮日志未记录有效的首 token 耗时；未知不代表 0。';
  return cell;
}

function headers(columns) {
  const row = document.createElement('tr');
  const sorting = prefs.tab === 'stats' ? { sort: 'day', direction: prefs.statsDirection } : prefs;
  for (const col of columns) {
    const cell = document.createElement('th');
    cell.className = col.numeric ? 'numeric' : '';
    if (col.label === '首字') cell.classList.add('response-column');
    if (col.title) cell.title = col.title;
    if (col.sort) {
      const button = document.createElement('button');
      button.className = 'header-sort';
      button.textContent = col.label;
      button.setAttribute('aria-label', '按' + col.label + '排序');
      if (sorting.sort === col.sort) {
        cell.setAttribute('aria-sort', sorting.direction === 'asc' ? 'ascending' : 'descending');
        const arrow = document.createElement('span');
        arrow.className = 'arrow'; arrow.textContent = sorting.direction === 'desc' ? '↓' : '↑';
        button.append(arrow);
      }
      button.addEventListener('click', () => {
        const direction = sorting.sort === col.sort && sorting.direction === 'desc' ? 'asc' : 'desc';
        if (prefs.tab === 'stats') prefs.statsDirection = direction;
        else { prefs.direction = direction; prefs.sort = col.sort; }
        page = 1; savePrefs(); load();
      });
      cell.append(button);
    } else cell.textContent = col.label;
    row.append(cell);
  }
  $('#data-table thead').replaceChildren(row);
}

function empty(columns, message = '当前范围没有完成记录', note = '调整日期，或点击表格上方的“返回全部记录”。') {
  const row = document.createElement('tr'), cell = td('', 'empty-cell');
  cell.colSpan = columns;
  const title = document.createElement('strong'); title.textContent = message;
  cell.append(title, document.createTextNode(note)); row.append(cell);
  $('#data-table tbody').replaceChildren(row);
}

function details(rows) {
  headers([
    { label: '工具' }, { label: '时间', sort: 'time' }, { label: '模型' },
    { label: '会话复用' },
    { label: '输入', numeric: true, title: '总输入 token，包含缓存输入' },
    { label: '输出', numeric: true, title: '输出 token，包含推理 token' },
    { label: '缓存命中', numeric: true }, { label: '成本', sort: 'cost', numeric: true },
    { label: '首字', sort: 'firstToken', numeric: true, title: responseDefinition },
    { label: '有效速度', sort: 'speed', numeric: true, title: '输出 token / 有效耗时；包含首字等待，排除后续工具耗时' }, { label: '详情' },
  ]);
  if (!rows.length) { empty(11); return; }
  $('#data-table tbody').replaceChildren(...rows.map((r) => {
    const row = document.createElement('tr');
    const action = td('');
    const button = document.createElement('button');
    button.className = 'icon-button view-record'; button.title = '查看记录详情'; button.setAttribute('aria-label', '查看记录详情');
    button.innerHTML = '<svg viewBox="0 0 24 24"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/></svg>';
    button.addEventListener('click', () => recordDialog(r)); action.append(button);
    const speed = td(r.speed == null ? DASH : '≈' + rate(r.speed), 'mono numeric speed-cell');
    if (r.speed != null) { const unit = document.createElement('span'); unit.className = 'speed-unit'; unit.textContent = 'tok/s'; speed.append(unit); }
    row.append(sourceCell(r), td(time(r.at, true), 'mono date-cell', time(r.at)), td(r.model, 'mono model-name', r.model),
      sessionCell(r), td(tokens(r.input), 'mono numeric token-input', full(r.input) + ' token'), td(tokens(r.output), 'mono numeric token-output', full(r.output) + ' token'),
      td(percent(r.cacheHit), 'mono numeric'), costCell(r), responseCell(r), speed, action);
    return row;
  }));
}

function projectName(path) {
  const parts = (path || '').replace(/\\/g, '/').replace(/\/+$/, '').split('/').filter(Boolean);
  if (!parts.length) return '未知项目';
  return parts.slice(-2).join('/');
}

function projects(rows) {
  headers([{ label: '项目' }, { label: '来源' }, { label: '请求', numeric: true },
    { label: '输入', numeric: true }, { label: '输出', numeric: true },
    { label: '缓存命中', numeric: true }, { label: '成本', numeric: true }]);
  if (!rows.length) { empty(7); return; }
  $('#data-table tbody').replaceChildren(...rows.map((item) => {
    const row = document.createElement('tr');
    const sources = Object.keys(item.sources || {}).join(' + ');
    const hit = item.input ? item.cache / item.input : null;
    row.append(
      td(projectName(item.project), 'mono project-cell', item.project || '未记录工作目录'),
      td(sources, 'source-cell'),
      td(full(item.records), 'mono numeric'),
      td(tokens(item.input), 'mono numeric token-input', full(item.input) + ' token'),
      td(tokens(item.output), 'mono numeric token-output', full(item.output) + ' token'),
      td(percent(hit), 'mono numeric'),
      costCell({ cost: item.cost, priced: item.priced, records: item.records }, true));
    return row;
  }));
}

function statistics(rows) {
  const labels = ['日期', '工具', '模型', '样本', '输入', '输出', '缓存命中', '成本', '首字', '平均', '最低', '最高'];
  headers(labels.map((label, i) => ({ label, sort: i === 0 ? 'day' : null, numeric: i >= 3, title: i === 3 ? '可测速记录 / 全部记录' : i === 8 ? responseDefinition + '\n中位数、P90（秒）及有效样本 / 可关联请求或轮次。' : i === 9 ? '加权平均速度，单位 tok/s' : i >= 10 ? '单条有效速度，单位 tok/s' : '' })));
  if (!rows.length) { empty(12); return; }
  $('#data-table tbody').replaceChildren(...rows.map((r) => {
    const row = document.createElement('tr');
    row.append(td(r.day, 'mono date-cell'), sourceCell(r), td(r.model, 'mono model-name', r.model), td(`${r.timed}/${r.records}`, 'mono numeric'),
      td(tokens(r.input), 'mono numeric token-input', full(r.input) + ' token'), td(tokens(r.output), 'mono numeric token-output', full(r.output) + ' token'),
      td(percent(r.cacheHit), 'mono numeric'), costCell(r, true), responseCell(r, true),
      ...[r.average, r.minimum, r.maximum].map((v, i) => td(v == null ? DASH : '≈' + rate(v), 'mono numeric ' + (i === 0 ? 'speed-cell' : ''), 'tok/s')));
    if (r.records) {
      row.className = 'stats-row'; row.tabIndex = 0; row.title = '聚焦当天、工具和模型的表格明细；日期、卡片与图表不变';
      const drill = () => {
        tableScope = { day: r.day, source: r.source || 'codex', model: r.model };
        prefs.tab = 'details'; page = 1; savePrefs(); controls(); load();
      };
      row.addEventListener('click', drill);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); drill(); } });
    }
    return row;
  }));
}

function pagination(total, stats = false) {
  const pages = Math.max(1, Math.ceil(total / prefs.pageSize));
  $('#total').textContent = `共 ${full(total)} ${stats ? '组' : '条'}${stats ? ' · 速度 tok/s · 首次响应 s' : ''}${tableScope && prefs.tab === 'details' ? ' · 表格聚焦' : ''}`;
  $('#page-label').textContent = `${page} / ${pages}`;
  $('#previous').disabled = page <= 1; $('#next').disabled = page >= pages;
}

function svgElement(tag, attributes = {}, text) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (text != null) element.textContent = text;
  return element;
}

function chartFrame(selector, dates, max, unit, label, format) {
  const width = Math.max(280, $(selector).clientWidth), height = $(selector).clientHeight;
  const left = 48, right = 20, top = 20, bottom = 32, baseline = height - bottom;
  const plotWidth = width - left - right;
  const x = (index) => left + (index + .5) * plotWidth / dates.length;
  const y = (value) => baseline - value * (baseline - top) / max;
  const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': label });
  svg.append(svgElement('text', { x: left, y: 10, fill: '#a4aec0', 'font-size': 10 }, unit));
  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4;
    svg.append(svgElement('line', { x1: left, x2: width - right, y1: y(value), y2: y(value), stroke: '#edf1f7', 'stroke-dasharray': '3 4' }));
    svg.append(svgElement('text', { x: left - 10, y: y(value) + 4, fill: '#a4aec0', 'text-anchor': 'end', 'font-size': 10 }, format(value)));
  }
  const count = Math.min(dates.length, Math.max(2, Math.floor(plotWidth / 85) + 1));
  for (let tick = 0; tick < count; tick++) {
    const i = count === 1 ? 0 : Math.round(tick * (dates.length - 1) / (count - 1));
    svg.append(svgElement('text', { x: x(i), y: height - 8, fill: '#9ca8bb', 'text-anchor': 'middle', 'font-size': 11 }, dates[i].slice(5)));
  }
  return { svg, x, y, baseline, plotWidth };
}

function chart() {
  const metric = prefs.metric;
  const response = metric.startsWith('response');
  const metricLabel = { average: '加权平均', median: '中位数', responseMedian: '中位数', responseP90: 'P90' }[metric];
  $('#trend-title').textContent = response ? '每日首次响应' : '每日速度趋势';
  $('#chart').setAttribute('aria-label', response ? '每日首次响应趋势图' : '每日速度趋势图');
  const c = commonCards;
  $('#trend-note').textContent = response ? `越低等待越短 · ${full(c?.responseSamples ?? 0)}/${full(c?.responseTurns ?? 0)} 个有效样本`
    : '虚线跨过无样本日期 · 点击图例隐藏系列';
  $('#trend-note').title = response ? responseDefinition + '\n虚线跨过无样本日期；点击图例隐藏系列。' : '';
  const series = new Map(), pointRows = new Map();
  const rows = commonRows;
  for (const r of rows) {
    if (!r.records || (response && !r.responseTurns) || r[metric] == null) continue;
    const key = [r.source || 'codex', r.model].join('\n');
    const label = `${sourceLabel(r.source)} · ${r.model}`;
    if (!series.has(key)) series.set(key, { label, points: new Map() });
    series.get(key).points.set(r.day, r[metric]);
    if (response) pointRows.set(key + '\n' + r.day, r);
  }
  const palette = ['#5279e1', '#9a7ddc', '#33a59e', '#dfad64', '#9babbc', '#d582a6'];
  const orderedSeries = [...series].sort(([, a], [, b]) => a.label.localeCompare(b.label));
  $('#legend').replaceChildren(...orderedSeries.map(([key, item], i) => {
    const button = document.createElement('button'), dot = document.createElement('i');
    dot.style.background = palette[i % palette.length]; button.append(dot, document.createTextNode(item.label));
    button.classList.toggle('inactive', hiddenSeries.has(key)); button.setAttribute('aria-pressed', String(!hiddenSeries.has(key)));
    button.addEventListener('click', () => { if (hiddenSeries.has(key)) hiddenSeries.delete(key); else hiddenSeries.add(key); chart(); });
    return button;
  }));
  const recordedDates = rows.filter((r) => r.records).map((r) => r.day).sort();
  const dates = [...new Set(rows.map((r) => r.day))].sort()
    .filter((day) => day >= recordedDates[0] && day <= recordedDates.at(-1));
  renderUsageChart({
    container: $('#usage-chart'), legend: $('#usage-legend'), summary: $('#usage-summary'),
    rows, metric: prefs.usageMetric, currency: prefs.currency,
  });
  const observed = orderedSeries.filter(([key]) => !hiddenSeries.has(key))
    .flatMap(([, item]) => [...item.points].filter(([, v]) => v != null));
  const values = observed.map(([, value]) => value);
  if (!values.length) {
    const message = document.createElement('div'); message.className = 'chart-empty';
    message.textContent = orderedSeries.length && orderedSeries.every(([key]) => hiddenSeries.has(key)) ? '点击图例恢复显示系列'
      : response ? '当前范围没有首 token 响应样本；轮次结束后可用，部分旧日志缺少此字段。' : '当前范围没有可展示的测速样本';
    $('#chart').replaceChildren(message); return;
  }
  const peak = Math.max(...values), step = Math.max(.1, 10 ** Math.floor(Math.log10((peak || .4) / 4)));
  const max = response ? Math.max(.4, Math.ceil(peak / 4 / step) * step * 4) : Math.max(10, Math.ceil(peak / 10) * 10);
  const unit = response ? 's' : 'tok/s';
  const { svg, x, y } = chartFrame('#chart', dates, max, unit,
    response ? `每日首次响应${metricLabel}，单位秒` : `每日${metricLabel}速度，单位 tok/s`,
    (value) => Number(value.toFixed(max < 2 ? 2 : 1)));
  const tooltip = document.createElement('div'); tooltip.className = 'chart-tooltip'; tooltip.hidden = true; tooltip.setAttribute('role', 'tooltip');
  const showTooltip = (point, title, detail) => {
    const heading = document.createElement('strong'); heading.textContent = title;
    const text = document.createElement('span'); text.textContent = detail;
    tooltip.replaceChildren(heading, text); tooltip.hidden = false;
    const bounds = $('#chart').getBoundingClientRect(), target = point.getBoundingClientRect();
    tooltip.style.left = Math.max(4, Math.min(target.x - bounds.x - tooltip.offsetWidth / 2, bounds.width - tooltip.offsetWidth - 4)) + 'px';
    tooltip.style.top = Math.max(0, Math.min(target.y - bounds.y - tooltip.offsetHeight - 10, bounds.height - tooltip.offsetHeight)) + 'px';
  };
  orderedSeries.forEach(([key, item], index) => {
    if (hiddenSeries.has(key)) return;
    const points = item.points;
    const color = palette[index % palette.length]; let path = '', previous = null;
    dates.forEach((date, i) => {
      const value = points.get(date);
      if (value == null) return;
      if (previous) {
        const segment = `M${x(previous.i)},${y(previous.value)} L${x(i)},${y(value)} `;
        const missing = i - previous.i - 1;
        if (missing) {
          const bridge = svgElement('path', { d: segment, fill: 'none', stroke: color, 'stroke-width': 2.3,
            'stroke-dasharray': '5 4', 'stroke-linecap': 'round', opacity: .7, 'data-gap': missing });
          bridge.append(svgElement('title', {}, `${item.label}\n${dates[previous.i]} — ${date}\n中间 ${missing} 天无有效${response ? '首次响应' : '测速'}数据，连接两端实测值`));
          svg.append(bridge);
        } else path += segment;
      }
      previous = { i, value };
    });
    if (path) svg.append(svgElement('path', { d: path, fill: 'none', stroke: color, 'stroke-width': 2.3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    dates.forEach((date, i) => {
      const value = points.get(date); if (value == null) return;
      const group = pointRows.get(key + '\n' + date);
      const label = `${metricLabel} ${response ? seconds(value) : rate(value)} ${unit}`;
      const detail = `${item.label}\n${label}` + (response ? `\n${group.responseSamples}/${group.responseTurns} 个有效样本` : '');
      const point = svgElement('circle', { cx: x(i), cy: y(value), r: 4.5, fill: color, stroke: 'white', 'stroke-width': 2,
        tabindex: 0, 'aria-label': `${date} ${detail}`, 'data-day': date, 'data-value': value, 'data-series': item.label });
      point.addEventListener('mouseenter', () => showTooltip(point, date, detail));
      point.addEventListener('focus', () => showTooltip(point, date, detail));
      point.addEventListener('mouseleave', () => { tooltip.hidden = true; });
      point.addEventListener('blur', () => { tooltip.hidden = true; });
      svg.append(point);
    });
  });
  $('#chart').replaceChildren(svg, tooltip);
}

async function loadCommon(force = false, retries = 0) {
  const key = commonKey();
  if (!force && commonCache?.key === key) return commonCache;
  const run = ++commonRequest;
  const params = { ...filters(null), page: 1, pageSize: 100 };
  const data = await send('stats', params);
  if (run !== commonRequest) return null;
  const rows = [...(data.rows || [])];
  let next = data.nextPage;
  while (next) {
    const more = await send('stats', { ...params, page: next });
    if (run !== commonRequest) return null;
    if (data.status?.version !== more.status?.version) {
      if (!retries) return loadCommon(true, 1);
      throw new Error('统计数据刚刚更新，请重新读取。');
    }
    rows.push(...(more.rows || [])); next = more.nextPage;
  }
  if (run !== commonRequest) return null;
  commonCache = { key, rows, cards: data.cards, status: data.status, version: data.status?.version };
  return commonCache;
}

function loadKey() {
  return JSON.stringify([commonKey(), prefs.tab, page, prefs.pageSize, prefs.sort,
    prefs.direction, prefs.statsDirection, tableScope]);
}

function load(options = {}) {
  const refreshCommon = !!options.refreshCommon, silent = !!options.silent, retries = options.retries || 0;
  const key = loadKey();
  if (!retries && !refreshCommon && activeLoad?.key === key) return activeLoad.promise;
  const run = { key };
  run.promise = loadView({ refreshCommon, silent, retries }).finally(() => { if (activeLoad === run) activeLoad = null; });
  activeLoad = run;
  return run.promise;
}

async function loadView(options = {}) {
  const { refreshCommon = false, silent = false, retries = 0 } = options;
  const current = ++sequence, tab = prefs.tab;
  const scroll = silent ? { x: window.scrollX, y: window.scrollY, left: $('#table-wrap').scrollLeft, top: $('#table-wrap').scrollTop } : null;
  controls(); if (!silent) $('#loading').hidden = false;
  const cached = commonCache?.key === commonKey();
  const needsCommon = refreshCommon || !cached;
  let cache = cached ? commonCache : null;
  let commonError = null;
  if (needsCommon) {
    try { cache = await loadCommon(refreshCommon, 0); }
    catch (error) { commonError = error; }
    if (current !== sequence) return;
  }
  try {
    if (cache) {
      commonRows = cache.rows; commonCards = cache.cards;
      snapshot = { status: cache.status || state.status, cards: cache.cards };
      if (cache.status) status({ ...state, status: cache.status });
      cards(cache.cards);
      if (needsCommon || !chartDrawn) { chart(); chartDrawn = true; }
    } else if (commonError) {
      commonRows = []; commonCards = null;
      cards(null); chart(); chartDrawn = true;
      showError(commonError.message);
    }

    if (tab === 'stats') {
      if (!cache) { headers([{ label: '日期' }]); empty(1, '本地统计暂不可用', '读取失败后点击刷新重试。'); pagination(0, true); return; }
      statsRows = [...commonRows].sort((a, b) => (prefs.statsDirection === 'asc' ? 1 : -1) * a.day.localeCompare(b.day));
      const pages = Math.max(1, Math.ceil(statsRows.length / prefs.pageSize));
      if (page > pages) page = 1;
      statistics(statsRows.slice((page - 1) * prefs.pageSize, page * prefs.pageSize));
      pagination(statsRows.length, true);
      return;
    }

    if (tab === 'projects') {
      const data = await send('projects', filters(null));
      if (current !== sequence) return;
      projects(data.rows || []);
      pagination(0, true);
      return;
    }

    const params = { ...filters(tableScope), page, pageSize: prefs.pageSize, sort: prefs.sort, direction: prefs.direction };
    const data = await send('details', params);
    if (current !== sequence) return;
    if (cache && data.status?.version !== cache.version) {
      if (!retries) return load({ refreshCommon: true, silent, retries: 1 });
      throw new Error('数据刚刚更新，请重新读取。');
    }
    if (page > Math.max(1, Math.ceil(data.total / prefs.pageSize))) {
      page = 1; return load({ silent });
    }
    snapshot = { status: data.status || snapshot?.status, cards: cache?.cards ?? snapshot?.cards };
    if (data.status) status({ ...state, status: data.status });
    details(data.rows);
    pagination(data.total, false);
  } catch (error) {
    if (current !== sequence) return;
    showError(error.message);
    if (!snapshot) { headers([{ label: '记录' }]); empty(1, '本地数据暂不可用', '安装本地读取器后，点击刷新数据。'); }
  } finally {
    if (current === sequence) {
      if (scroll) { $('#table-wrap').scrollTo(scroll.left, scroll.top); window.scrollTo(scroll.x, scroll.y); }
      $('#loading').hidden = true;
    }
  }
}

function recordDialog(r) {
  const buddy = r.sourceId && r.sourceId !== 'codex';
  const sourceId = r.sourceId || r.source || 'codex';
  const entries = [
    ['工具', sourceLabel(sourceId)], ['完成时间', time(r.at)], ['模型', r.model],
  ];
  if (buddy) {
    entries.push(
      ['输入', full(r.input) + ' token（包含缓存输入，不重复相加）'],
      ['输出', full(r.output) + ' token'],
      ['缓存命中', r.cacheKnown ? percent(r.cacheHit) : '—（字段缺失或未知）'],
      ['日志 credit', r.creditValid ? credit(r.credit) : '—（无有效原值）'],
      ['credit 口径', '日志原值；不换算美元/人民币，不推算订阅余额'],
      ['成本', r.cost == null ? '—（价格或缓存未知，未计价；不按 0 计算）' : costText(r.cost)],
      ['计价规则', r.pricingBasis || '价格或缓存未知，无法计价'],
      ['参考价版本', r.pricingVersion],
      ['有效耗时', r.duration == null ? DASH : r.duration.toFixed(3) + ' 秒'],
      ['有效速度', r.speed == null ? DASH : '≈' + rate(r.speed) + ' tok/s'],
      ['耗时依据', r.timingSource === 'trace' ? '生成追踪的开始与结束时间' : r.timingSource === 'boundary' ? '会话请求边界估算，包含等待及推理，排除后续工具执行' : '缺少可关联的生成追踪或会话边界'],
      ['首字', r.firstToken == null ? '—（本来源未提供该条首字；未知不计为 0）' : r.firstToken.toFixed(3) + ' 秒'],
      ['首字口径', 'WorkBuddy/CLI 按每次模型请求记录首字；与 Codex 每轮仅首个请求计一次不同。缺失不计为零。'],
      ['首字来源', firstTokenSourceLabel(r.firstTokenSource)],
      ['请求编号', r.messageId || r.requestId || r.key],
      ['会话 ID', r.sessionId || DASH],
    );
    if (r.session) {
      entries.push(
        ['最近运行返回编号', r.session.runs?.[0]?.actual || '未找到返回证据'],
        ['最近运行请求续接编号', r.session.requested || (r.session.runCount ? '未请求续接' : '未记录')],
        ['复用结论', sessionLabel(r.session)],
        ['关联运行', full(r.session.runCount) + ' 次'],
        ['已成功匹配续接', full(r.session.confirmedResumeCount) + ' 次'],
        ['续接返回编号一致', full(r.session.matchedResumeCount ?? r.session.confirmedResumeCount) + ' 次（执行结果另列）'],
      );
      if (r.session.requestedValues?.length > 1) {
        entries.push(['续接编号记录', r.session.requestedValues.join('、')]);
      }
      const outcomeLabel = { resumed: '续接已证实', new: '新会话', mismatch: '不匹配',
        failed: '失败', unconfirmed: '未证实', pending: '运行中/待定', unknown: '未知' };
      const evidence = (r.session.runs || []).map((run, index) =>
        `${index + 1}. ${run.runId || '运行'} · 请求续接 ${run.requested || '新会话'} → 实际返回 ${run.actual || '未知'} · ${outcomeLabel[run.outcome] || run.outcome}${run.status ? ' · ' + run.status : ''}`);
      if (evidence.length) entries.push(['关联运行证据', evidence.join('\n') +
        (r.session.runsTruncated ? `\n仅展示最近 ${evidence.length} 次；上方计数覆盖全部 ${r.session.runCount} 次运行。` : '')]);
      entries.push(['上下文压缩', '未知；会话共用不等于未压缩，autocompact 是工作预算，不是物理窗口']);
    }
    entries.push(['记录 ID', r.key], ['来源文件', r.source || DASH]);
  } else {
    entries.push(
      ['输入', full(r.input) + ' token'], ['输出', full(r.output) + ' token'],
      ['缓存命中', percent(r.cacheHit)],
      ['有效耗时', r.duration == null ? DASH : r.duration.toFixed(2) + ' 秒'],
      ['有效速度', r.speed == null ? DASH : '≈' + rate(r.speed) + ' tok/s'],
      ['成本', r.cost == null ? '—（未知，不按 0 或免费计算）' : costText(r.cost)],
      ['首字', r.firstToken == null
        ? (r.firstInTurn ? '—（本轮首个请求未记录有效首字；未知不计 0）' : '—（非本轮首个请求）')
        : r.firstToken.toFixed(3) + ' 秒'],
      ['首字口径', 'Codex 每轮仅首个请求记录一次。' + (r.firstInTurn ? '此记录为本轮首个请求。' : '此记录不是本轮首个请求，故不单独计首字。')],
      ['响应口径', responseDefinition],
      ['计价规则', r.cost == null ? '此记录无法定价' : (r.longContext ? '输入 >272K：输入及缓存 2×，输出 1.5×' : '输入 ≤272K：标准参考价') + (r.mode === 'fast' ? '；Fast 再乘 2×' : '')],
      ['参考价版本', r.pricingVersion],
      ['记录 ID', r.key], ['会话 ID', r.sessionId], ['来源文件', r.source],
    );
    if (r.turnId) entries.splice(entries.length - 3, 0, ['轮次 ID', r.turnId]);
  }
  $('#detail-fields').replaceChildren(...entries.flatMap(([label, value]) => {
    const term = document.createElement('dt'), description = document.createElement('dd');
    term.textContent = label; description.textContent = value;
    if (label === '关联运行证据') description.style.whiteSpace = 'pre-line';
    return [term, description];
  }));
  $('#detail-dialog').showModal();
}

function settings() {
  $('#home').value = state.status?.home || '';
  $('#workbuddy').value = state.status?.buddy?.roots?.workbuddy || '';
  $('#codebuddy').value = state.status?.buddy?.roots?.codebuddy || '';
  $('#bridge').value = state.status?.buddy?.roots?.bridge || '';
  $('#refresh-interval').value = String(state.refreshMinutes || 1);
  $('#currency').value = prefs.currency;
  $('#setting-error').textContent = '';
  if (!state.status) {
    $('#connection-info').textContent = '读取器尚未连接。请运行 install.ps1，然后点击刷新。';
    $('#health-info').textContent = '';
    $('#health-info').classList.remove('health-warn');
  } else {
    const parts = [`Codex ${full(state.status.records)} 条`];
    for (const item of state.status.sources || []) {
      if (item.id === 'codex') continue;
      parts.push(`${item.label}：${item.available ? (item.records ? full(item.records) + ' 条' : '已发现目录，尚无记录') : '未发现来源'}`);
    }
    const bridge = state.status.buddy?.bridge;
    if (bridge) {
      parts.push(`桥接运行：${bridge.exists ? `${full(bridge.runs)} 次 / ${full(bridge.sessions)} 个会话` : '未发现来源'}`);
    }
    $('#connection-info').textContent = `已建立本地索引：${parts.join('；')}。参考价版本 ${state.status.pricingVersion}。`;
    const unparsed = (state.status.health?.skipped ?? 0) + (state.status.health?.invalid ?? 0);
    const health = $('#health-info');
    health.classList.toggle('health-warn', unparsed > 0);
    if (!unparsed) {
      health.textContent = '数据健康：所有日志行均已计入统计。';
    } else {
      const detail = (state.status.sources || [])
        .filter((item) => (item.skipped || 0) + (item.invalid || 0) > 0)
        .map((item) => `${item.label} ${full((item.skipped || 0) + (item.invalid || 0))} 行`);
      health.textContent = `数据健康：${full(unparsed)} 行无法解析，未计入统计（${detail.join('；')}）。若持续增加，可能是日志格式变更。`;
    }
  }
  $('#settings-dialog').showModal();
}

for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click', () => {
  prefs.tab = button.dataset.tab;
  if (prefs.tab !== 'details') tableScope = null;
  page = 1; savePrefs(); load();
});
for (const name of ['preset', 'from', 'to']) $('#' + name).addEventListener('change', () => {
  if (name === 'from' || name === 'to') {
    prefs.from = $('#from').value; prefs.to = $('#to').value;
  } else prefs.preset = $('#preset').value;
  tableScope = null; page = 1; controls(); savePrefs(); load({ refreshCommon: true });
});
$('#metric').addEventListener('change', () => { prefs.metric = $('#metric').value; savePrefs(); chart(); });
$('#usage-metric').addEventListener('change', () => { prefs.usageMetric = $('#usage-metric').value; savePrefs(); chart(); });
$('#page-size').addEventListener('change', () => { prefs.pageSize = Number($('#page-size').value); page = 1; savePrefs(); load(); });
$('#previous').addEventListener('click', () => { page--; load(); });
$('#next').addEventListener('click', () => { page++; load(); });
$('#clear-scope').addEventListener('click', () => { tableScope = null; page = 1; controls(); load(); });
$('#refresh').addEventListener('click', async () => {
  $('#refresh').disabled = true; $('#refresh').classList.add('busy'); $('#refresh span').textContent = '正在刷新';
  try {
    await send('refresh');
    status(await send('state'));
    await load({ refreshCommon: true });
  }
  catch (error) { status({ ...state, busy: false, error: error.message }); }
});
$('#quota-refresh').addEventListener('click', async () => {
  state.quotaBusy = true; renderQuota(state.quota, true);
  try { await send('quota'); status(await send('state')); }
  catch (error) { state.quotaBusy = false; state.quota = { ...state.quota, error: error.message }; renderQuota(state.quota, false); }
});
$('#settings').addEventListener('click', settings); $('#error-settings').addEventListener('click', settings);
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $('#' + button.dataset.close).close());
$('#save-home').addEventListener('click', async () => {
  $('#save-home').disabled = true; $('#setting-error').textContent = '';
  try {
    await send('configure', {
      home: $('#home').value, refreshMinutes: Number($('#refresh-interval').value),
      workbuddy: $('#workbuddy').value, codebuddy: $('#codebuddy').value,
      bridge: $('#bridge').value,
    });
    prefs.currency = $('#currency').value;
    await savePrefs();
    status(await send('state')); $('#settings-dialog').close();
    cards(commonCards); chart();
    await load();
  }
  catch (error) { $('#setting-error').textContent = error.message; }
  finally { $('#save-home').disabled = false; }
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes.monitorState) return;
  const next = changes.monitorState.newValue;
  status(next);
  if (booted) updateView();
});
function updateView() {
  const roots = JSON.stringify(state.status?.buddy?.roots ?? null);
  const previous = JSON.stringify(snapshot?.status?.buddy?.roots ?? null);
  if (!state.busy && state.status && (state.status.version !== snapshot?.status?.version
      || state.status.home !== snapshot?.status?.home || roots !== previous)) return load({ refreshCommon: true, silent: !!snapshot });
}
function refreshVisible() {
  if (booted && document.visibilityState === 'visible') send('visit').catch((error) => showError(error.message));
}
document.addEventListener('visibilitychange', refreshVisible);
window.addEventListener('resize', () => { chart(); });
setInterval(() => renderQuota(state.quota ?? state.status?.quota, !!state.quotaBusy), 60000);

async function boot() {
  const saved = await chrome.storage.local.get(['preferences', 'monitorState']);
  if (saved.preferences) Object.assign(prefs, saved.preferences);
  delete prefs.source; delete prefs.model; delete prefs.mode;
  if (!['details', 'stats'].includes(prefs.tab)) prefs.tab = 'details';
  if (!['1', '3', '7', '30', 'custom'].includes(String(prefs.preset))) prefs.preset = '3';
  if (![20, 50, 100].includes(Number(prefs.pageSize))) prefs.pageSize = 50;
  if (!['USD', 'CNY'].includes(prefs.currency)) prefs.currency = 'USD';
  if (!['average', 'median', 'responseMedian', 'responseP90'].includes(prefs.metric)) prefs.metric = 'average';
  if (!['output', 'input', 'records', 'cost'].includes(prefs.usageMetric)) prefs.usageMetric = 'output';
  prefs.statsDirection = prefs.statsDirection === 'asc' ? 'asc' : 'desc';
  const initial = range(3); $('#from').value = prefs.from || initial.from; $('#to').value = prefs.to || initial.to;
  controls();
  status(saved.monitorState);
  await savePrefs();
  try { status(await send('state')); } catch (error) { showError(error.message); }
  await load({ refreshCommon: true });
  booted = true;
  refreshVisible();
}
boot().catch((error) => showError(error.message));
