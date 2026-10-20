import { DASH, USD_CNY, full, money, tokens } from './format.mjs';

export const MODEL_PALETTE = ['#5279e1', '#9a7ddc', '#33a59e', '#dfad64', '#9babbc', '#d582a6', '#5aa9d6', '#c5905d'];

const SVG_NS = 'http://www.w3.org/2000/svg';
let renderId = 0;

export function modelColor(model, models) {
  const index = models.indexOf(model);
  if (index < MODEL_PALETTE.length) return MODEL_PALETTE[Math.max(0, index)];
  return `hsl(${(Math.max(0, index) * 47) % 360} 52% 56%)`;
}

export function usageMetricLabel(metric, currency = 'USD') {
  return {
    output: '输出 token', input: '输入 token', records: '请求记录',
    cost: `成本 · ${currency}`,
  }[metric] ?? metric;
}

export function usageUnit(metric) {
  return { output: 'token', input: 'token', records: '条', cost: '' }[metric] ?? '';
}

const converted = (value, currency) => value == null ? null : value * (currency === 'CNY' ? USD_CNY : 1);

export function compactMoney(value, currency = 'USD') {
  if (value == null) return DASH;
  const amount = converted(value, currency);
  const digits = amount >= 1 ? 2 : amount >= .01 ? 3 : 4;
  return '~' + (currency === 'CNY' ? '¥' : '$') + amount.toFixed(digits);
}

export function formatUsageValue(value, metric, currency = 'USD', compact = false) {
  if (value == null) return DASH;
  if (metric === 'cost') return compact ? compactMoney(value, currency) : money(value, currency);
  if (metric === 'records') return full(value) + ' 条';
  return compact ? tokens(value) : full(value) + ' token';
}

export function formatUsageAxis(value, metric, currency = 'USD') {
  if (metric === 'cost') return compactMoney(value, currency);
  if (metric === 'records') return full(value);
  return tokens(value);
}

function niceStep(value) {
  if (!(value > 0)) return 1;
  const exponent = 10 ** Math.floor(Math.log10(value));
  const fraction = value / exponent;
  return exponent * (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10);
}

export function niceUsageMax(peak, metric = 'output') {
  if (!(peak > 0)) return metric === 'cost' ? .01 : metric === 'records' ? 10 : 100;
  const step = niceStep(peak / 4);
  return Math.max(step, Math.ceil(peak / step) * step);
}

/**
 * Aggregate daily stats rows into one stack per day, keyed by model only.
 * Tool identity is intentionally not part of the segment key: the same model
 * shown by Codex, WorkBuddy and CLI contributes to one segment.
 */
export function aggregateUsage(rows, { metric = 'output', currency = 'USD' } = {}) {
  const byDay = new Map();
  for (const row of rows || []) {
    if (!row?.day) continue;
    if (!byDay.has(row.day)) {
      byDay.set(row.day, {
        day: row.day, total: 0, records: 0, priced: 0, unknownRecords: 0,
        costKnown: false, segments: new Map(),
      });
    }
    const day = byDay.get(row.day);
    const records = Number(row.records) || 0;
    const model = row.model || DASH;
    if (!day.segments.has(model)) {
      day.segments.set(model, {
        model, value: 0, records: 0, priced: 0, unknownRecords: 0, costKnown: false,
      });
    }
    const segment = day.segments.get(model);
    segment.records += records;
    day.records += records;
    if (metric === 'cost') {
      const priced = row.priced == null
        ? (row.cost == null ? 0 : records) : Math.max(0, Math.min(records, Number(row.priced) || 0));
      const unknown = Math.max(0, records - priced);
      segment.priced += priced;
      segment.unknownRecords += unknown;
      day.priced += priced;
      day.unknownRecords += unknown;
      if (priced > 0) {
        const value = Number(row.cost) || 0;
        segment.value += value;
        segment.costKnown = true;
        day.total += value;
        day.costKnown = true;
      }
    } else {
      const value = Number(row[metric]) || 0;
      segment.value += value;
      day.total += value;
    }
  }

  const days = [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day)).map((day) => ({
    ...day,
    partial: metric === 'cost' && day.priced > 0 && day.unknownRecords > 0,
    allUnknown: metric === 'cost' && day.priced === 0 && day.unknownRecords > 0,
    segments: [...day.segments.values()].sort((a, b) => a.model.localeCompare(b.model)),
  }));
  const models = [...new Set(days.flatMap((day) => day.segments.map((segment) => segment.model)))]
    .sort((a, b) => a.localeCompare(b));
  const peak = Math.max(0, ...days.map((day) => day.total));
  return {
    metric, currency, days, models, peak,
    records: days.reduce((sum, day) => sum + day.records, 0),
    priced: days.reduce((sum, day) => sum + day.priced, 0),
    unknownRecords: days.reduce((sum, day) => sum + day.unknownRecords, 0),
    total: days.reduce((sum, day) => sum + day.total, 0),
  };
}

function svgElement(tag, attributes = {}, text) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (text != null) element.textContent = text;
  return element;
}

function tooltipLines(day, metric, currency, models) {
  const lines = [];
  const date = day.day;
  if (metric === 'cost') {
    if (day.priced === 0 && day.unknownRecords) {
      lines.push(['total', `全部 ${full(day.unknownRecords)} 条未计价（未知，不计为 0）`]);
    } else {
      lines.push(['total', `已知合计 ${money(day.total, currency)}` +
        (day.unknownRecords ? ` · ${full(day.unknownRecords)} 条未计价` : ' · 全部记录已计价')]);
    }
  } else {
    lines.push(['total', `合计 ${formatUsageValue(day.total, metric, currency)}`]);
  }
  for (const segment of day.segments) {
    let detail;
    if (metric === 'cost') {
      detail = segment.priced === 0 && segment.unknownRecords
        ? `未计价 · 0/${full(segment.records)} 条已计价 · ${full(segment.unknownRecords)} 条未知`
        : `${formatUsageValue(segment.value, metric, currency)} · ${full(segment.priced)}/${full(segment.records)} 条已计价` +
          (segment.unknownRecords ? ` · ${full(segment.unknownRecords)} 条未知`
            : segment.value === 0 ? ' · 已知 0' : '');
    } else {
      detail = `${formatUsageValue(segment.value, metric, currency)} · ${full(segment.records)} 条记录`;
    }
    lines.push(['model', segment.model, detail, modelColor(segment.model, models)]);
  }
  lines.push(['records', `${full(day.records)} 条请求记录`]);
  if (metric === 'cost' && day.unknownRecords) {
    lines.push(['note', '未知表示未计价，不按 $0 或免费计算；斜线为含未计价记录。']);
  }
  return lines;
}

function tooltipText(date, lines) {
  return [date, ...lines.map((line) => line[0] === 'model'
    ? `${line[1]} ${line[2]}`
    : line.slice(1).join(' '))].join('\n');
}

function tooltipNode(date, lines) {
  const title = document.createElement('strong');
  title.textContent = date;
  const body = document.createElement('div');
  body.className = 'usage-tooltip-lines';
  for (const [kind, ...parts] of lines) {
    const line = document.createElement('span');
    line.className = `usage-tip-${kind}`;
    if (kind === 'model') {
      const swatch = document.createElement('i');
      swatch.style.background = parts[2];
      const name = document.createElement('b');
      name.textContent = parts[0];
      const value = document.createElement('em');
      value.textContent = parts[1];
      line.append(swatch, name, value);
    } else {
      line.textContent = parts.join(' ');
    }
    body.append(line);
  }
  return [title, body];
}

export function renderUsageChart({ container, legend, summary, rows, metric = 'output', currency = 'USD' }) {
  const data = aggregateUsage(rows, { metric, currency });
  const label = usageMetricLabel(metric, currency);
  if (summary) {
    if (!data.days.length) summary.textContent = '当前范围没有完成的用量记录';
    else if (metric === 'cost') {
      summary.textContent = data.priced === 0 && data.unknownRecords
        ? `全部 ${full(data.unknownRecords)} 条未计价（未知，不计为 0）`
        : `已知合计 ${money(data.total, currency)}` +
          (data.unknownRecords ? ` · ${full(data.unknownRecords)} 条未计价（未知，不计为 0）` : ' · 全部记录已计价');
    } else {
      summary.textContent = `合计 ${formatUsageValue(data.total, metric, currency)} · ${full(data.records)} 条请求记录`;
    }
  }
  if (legend) {
    legend.replaceChildren(...data.models.map((model) => {
      const item = document.createElement('span');
      item.className = 'legend-item';
      const dot = document.createElement('i');
      dot.style.background = modelColor(model, data.models);
      item.append(dot, document.createTextNode(model));
      return item;
    }));
    if (metric === 'cost' && data.unknownRecords) {
      const item = document.createElement('span');
      item.className = 'legend-item legend-unknown';
      const dot = document.createElement('i');
      item.append(dot, document.createTextNode('含未计价记录'));
      legend.append(item);
    }
  }
  if (!data.days.length) {
    const message = document.createElement('div');
    message.className = 'chart-empty';
    message.textContent = '当前范围没有完成的用量记录';
    container.replaceChildren(message);
    return;
  }

  const width = Math.max(280, container.clientWidth || 640);
  const height = Math.max(150, container.clientHeight || 210);
  const left = 44, right = 16, top = 18, bottom = 30, baseline = height - bottom;
  const plotWidth = width - left - right;
  const band = plotWidth / data.days.length;
  const barWidth = Math.max(2, Math.min(30, band * .58));
  const max = niceUsageMax(data.peak, metric);
  const center = (index) => left + (index + .5) * band;
  const y = (value) => baseline - value / max * (baseline - top);
  const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': `每日${label}，按模型堆叠` });
  const unit = metric === 'cost' ? (currency === 'CNY' ? '人民币' : '美元') : usageUnit(metric);
  if (unit) svg.append(svgElement('text', { x: left, y: 10, fill: '#a4aec0', 'font-size': 10 }, unit));

  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4;
    svg.append(svgElement('line', { x1: left, x2: width - right, y1: y(value), y2: y(value),
      stroke: '#edf1f7', 'stroke-dasharray': '3 4' }));
    svg.append(svgElement('text', { x: left - 8, y: y(value) + 4, fill: '#a4aec0',
      'text-anchor': 'end', 'font-size': 10 }, formatUsageAxis(value, metric, currency)));
  }

  const tickCount = Math.min(data.days.length, Math.max(2, Math.floor(plotWidth / 70) + 1));
  for (let tick = 0; tick < tickCount; tick++) {
    const index = tickCount === 1 ? 0 : Math.round(tick * (data.days.length - 1) / (tickCount - 1));
    svg.append(svgElement('text', { x: center(index), y: height - 8, fill: '#9ca8bb',
      'text-anchor': 'middle', 'font-size': 11 }, data.days[index].day.slice(5)));
  }

  const patternId = `usage-hatch-${++renderId}`;
  const defs = svgElement('defs');
  const pattern = svgElement('pattern', { id: patternId, width: 6, height: 6,
    patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
  pattern.append(svgElement('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: '#d8a85f', 'stroke-width': 2 }));
  defs.append(pattern);
  svg.append(defs);

  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip usage-tooltip';
  tooltip.hidden = true;
  tooltip.setAttribute('role', 'tooltip');
  const show = (target, day) => {
    const lines = tooltipLines(day, metric, currency, data.models);
    tooltip.replaceChildren(...tooltipNode(day.day, lines));
    tooltip.hidden = false;
    const bounds = container.getBoundingClientRect();
    const box = target.getBoundingClientRect();
    tooltip.style.left = Math.max(4, Math.min(box.x - bounds.x + box.width / 2 - tooltip.offsetWidth / 2,
      bounds.width - tooltip.offsetWidth - 4)) + 'px';
    tooltip.style.top = Math.max(0, Math.min(box.y - bounds.y - tooltip.offsetHeight - 10,
      bounds.height - tooltip.offsetHeight)) + 'px';
  };

  data.days.forEach((day, index) => {
    const x = Math.min(width - right - barWidth, Math.max(left, center(index) - barWidth / 2));
    const group = svgElement('g', { class: 'usage-day', 'data-day': day.day, 'data-value': day.total,
      'data-total': day.total, 'data-records': day.records, 'data-priced': day.priced,
      'data-unknown': day.unknownRecords, tabindex: 0, role: 'group' });
    const lines = tooltipLines(day, metric, currency, data.models);
    group.setAttribute('aria-label', tooltipText(day.day, lines));
    group.append(svgElement('rect', { class: 'usage-hit', x: left + index * band, y: top,
      width: band, height: baseline - top, fill: 'transparent' }));
    let cursor = baseline;
    for (const segment of day.segments) {
      if (!(segment.value > 0)) continue;
      const segmentHeight = segment.value / max * (baseline - top);
      const segmentY = cursor - segmentHeight;
      const rect = svgElement('rect', { class: 'usage-segment', x, y: segmentY, width: barWidth,
        height: Math.max(1, segmentHeight), rx: 1.5, fill: modelColor(segment.model, data.models),
        'data-model': segment.model, 'data-value': segment.value, 'data-known': String(segment.costKnown),
        'data-unknown-records': segment.unknownRecords });
      rect.append(svgElement('title', {}, `${segment.model}\n${formatUsageValue(segment.value, metric, currency)}`));
      group.append(rect);
      if (metric === 'cost' && segment.unknownRecords) {
        group.append(svgElement('rect', { class: 'usage-segment-unknown', x, y: segmentY,
          width: barWidth, height: Math.max(1, segmentHeight), rx: 1.5, fill: `url(#${patternId})`,
          'pointer-events': 'none' }));
      }
      cursor = segmentY;
    }
    if (metric === 'cost' && day.unknownRecords) {
      const capY = Math.max(top, (day.total > 0 ? y(day.total) : baseline) - 5);
      group.append(svgElement('rect', { class: 'usage-cap-base', x, y: capY, width: barWidth,
        height: 3, rx: 1.5, fill: '#f7f8fb', 'pointer-events': 'none' }));
      group.append(svgElement('rect', { class: 'usage-unknown-cap', x, y: capY, width: barWidth,
        height: 3, rx: 1.5, fill: `url(#${patternId})`, 'pointer-events': 'none' }));
      group.append(svgElement('text', { class: 'usage-unknown-mark', x: x + barWidth / 2,
        y: capY - 2, 'text-anchor': 'middle', fill: '#b6833e', 'font-size': 9,
        'pointer-events': 'none' }, '?'));
    }
    group.addEventListener('mouseenter', () => show(group, day));
    group.addEventListener('focus', () => show(group, day));
    group.addEventListener('mouseleave', () => { tooltip.hidden = true; });
    group.addEventListener('blur', () => { tooltip.hidden = true; });
    svg.append(group);
  });

  container.replaceChildren(svg, tooltip);
}
