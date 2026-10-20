import { DASH, full, time } from './format.mjs';
import { buildQuotaRing, ringSegmentText, RING_PALETTE } from './quota-ring.mjs';

export function windowLabel(minutes, kind) {
  if (minutes === 10080) return '每周额度';
  if (minutes === 1440) return '每日额度';
  if (minutes == null) return kind === 'primary' ? '主要额度' : '次要额度';
  if (minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时额度`;
  return `${minutes} 分钟额度`;
}

export const quotaPercent = (value) => value == null ? DASH : Number(value.toFixed(1)) + '%';

export function resetCountdown(at, now = Date.now()) {
  if (at == null) return '重置时间未知';
  const minutes = Math.ceil((at * 1000 - now) / 60000);
  if (minutes <= 0) return '已到重置时间，等待额度更新';
  if (minutes >= 1440) return `约 ${Math.floor(minutes / 1440)} 天 ${Math.floor(minutes % 1440 / 60)} 小时后重置`;
  if (minutes >= 60) return `约 ${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟后重置`;
  return `${minutes} 分钟后重置`;
}

const BUDDY_STATUS = {
  known: ['已同步', 'known'],
  stale: ['可能过期', 'stale'],
  error: ['读取失败', 'error'],
  unknown: ['额度未知', 'unknown'],
};

const creditText = (value) => value == null ? '未知' : full(value);
const buddyState = (buddy) => buddy?.status in BUDDY_STATUS ? buddy.status : 'unknown';
const sourceLabel = (buddy) => buddy?.source?.label || (buddy?.source?.kind ? buddy.source.kind : '来源未标注');
const creditSummary = (buddy) => {
  const parts = [];
  if (buddy.remaining != null) parts.push(`剩余 ${creditText(buddy.remaining)} ${buddy.unit || 'credit'}`);
  if (buddy.total != null) parts.push(`总量 ${full(buddy.total)}`);
  if (buddy.used != null) parts.push(`已用 ${full(buddy.used)}`);
  if (buddy.frozen != null && buddy.frozen !== 0) parts.push(`冻结 ${full(buddy.frozen)}`);
  return parts.length ? parts.join(' · ') : '额度数值未知';
};
const secondarySummary = (buddy) => {
  const parts = [];
  if (buddy.total != null) parts.push(`总量 ${full(buddy.total)}`);
  if (buddy.used != null) parts.push(`已用 ${full(buddy.used)}`);
  if (buddy.frozen != null && buddy.frozen !== 0) parts.push(`冻结 ${full(buddy.frozen)}`);
  return parts.join(' · ');
};

const SVG_NS = 'http://www.w3.org/2000/svg';
function svgElement(tag, attributes = {}, text) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (text != null) element.textContent = text;
  return element;
}

function sideHead(title, meta, status) {
  const head = document.createElement('div'); head.className = 'quota-side-head';
  const left = document.createElement('div'); left.className = 'quota-side-title';
  left.textContent = title;
  head.append(left);
  if (status) head.append(status);
  else if (meta) {
    const label = document.createElement('span'); label.className = 'quota-side-meta';
    label.textContent = meta; head.append(label);
  }
  return head;
}

function codexRow(bucket, window, buckets) {
  const row = document.createElement('div');
  row.className = 'codex-window-row'; row.dataset.window = window.kind || '';
  const head = document.createElement('div'); head.className = 'codex-window-head';
  const name = document.createElement('span'); name.className = 'codex-window-name';
  name.textContent = buckets.length > 1 ? `${bucket.name} · ${windowLabel(window.minutes, window.kind)}`
    : windowLabel(window.minutes, window.kind);
  const plan = document.createElement('span'); plan.className = 'peer-plan';
  plan.textContent = bucket.plan ? bucket.plan.toUpperCase() : '';
  plan.hidden = !bucket.plan;
  const remaining = document.createElement('span'); remaining.className = 'codex-window-remaining';
  remaining.textContent = `剩余 ${quotaPercent(window.remaining)}`;
  head.append(name, plan, remaining);
  const bar = document.createElement('div');
  bar.className = 'codex-window-bar' + (window.remaining == null ? ' unknown' : '');
  const fill = document.createElement('i');
  if (window.remaining != null) fill.style.width = Math.max(0, Math.min(100, window.remaining)) + '%';
  bar.append(fill);
  const meta = document.createElement('div'); meta.className = 'codex-window-meta';
  const used = document.createElement('span'); used.textContent = `已使用 ${quotaPercent(window.used)}`;
  const reset = document.createElement('span');
  reset.textContent = window.resetAt == null ? '重置时间未知'
    : `重置 ${time(window.resetAt, true)} · ${resetCountdown(window.resetAt)}`;
  meta.append(used, reset);
  row.append(head, bar, meta);
  return row;
}

function codexPanel(quota, busy) {
  const panel = document.createElement('section'); panel.className = 'quota-codex';
  const meta = quota?.fetchedAt
    ? `${quota.source === 'log' ? '日志记录' : '账号接口'} · 更新 ${time(quota.fetchedAt, true)}`
    : busy ? '正在读取…' : '尚未读取';
  panel.append(sideHead('Codex', meta));
  const rows = document.createElement('div'); rows.className = 'codex-rows';
  const buckets = quota?.buckets ?? [];
  for (const bucket of buckets) for (const window of bucket.windows || []) {
    rows.append(codexRow(bucket, window, buckets));
  }
  if (!rows.childElementCount) {
    const empty = document.createElement('p'); empty.className = 'quota-side-empty';
    empty.textContent = busy ? '正在读取 Codex 额度…' : 'Codex 额度尚未读取。';
    rows.append(empty);
  }
  panel.append(rows);
  return panel;
}

function ringSvg(ring, unit, panel) {
  const size = 112, center = 56, radius = 43, stroke = 13;
  const circumference = 2 * Math.PI * radius;
  const svg = svgElement('svg', { viewBox: `0 0 ${size} ${size}`, class: 'ring-svg',
    role: 'img', 'aria-label': `WorkBuddy 剩余额度 ${creditText(ring.total)} ${unit}，按到期时间分组` });
  svg.append(svgElement('circle', { cx: center, cy: center, r: radius, fill: 'none',
    stroke: '#edf1f8', 'stroke-width': stroke }));
  const tooltip = document.createElement('div');
  tooltip.className = 'ring-tooltip'; tooltip.hidden = true; tooltip.setAttribute('role', 'tooltip');
  panel.append(tooltip);
  let offset = 0;
  ring.segments.forEach((segment, index) => {
    const length = segment.value / ring.total * circumference;
    const gap = ring.segments.length > 1 ? Math.min(2.2, length * .22) : 0;
    const dash = Math.max(.8, length - gap);
    const color = RING_PALETTE[index % RING_PALETTE.length];
    const circle = svgElement('circle', {
      class: 'ring-segment', cx: center, cy: center, r: radius, fill: 'none', stroke: color,
      'stroke-width': stroke, 'stroke-dasharray': `${dash} ${circumference - dash}`,
      'stroke-dashoffset': -offset, transform: `rotate(-90 ${center} ${center})`, tabindex: 0,
      'data-key': segment.key, 'data-value': segment.value, 'data-label': segment.label,
      'aria-label': ringSegmentText(segment, unit),
    });
    circle.addEventListener('mouseenter', () => showRingTip(panel, tooltip, circle, segment, unit));
    circle.addEventListener('focus', () => showRingTip(panel, tooltip, circle, segment, unit));
    circle.addEventListener('mouseleave', () => { tooltip.hidden = true; });
    circle.addEventListener('blur', () => { tooltip.hidden = true; });
    svg.append(circle);
    offset += length;
  });
  return svg;
}

function showRingTip(panel, tooltip, target, segment, unit) {
  const title = document.createElement('strong');
  title.textContent = segment.date ? `${segment.label} · ${segment.date}`
    : segment.range ? `${segment.label} · ${segment.range}` : segment.label;
  const sum = document.createElement('span');
  sum.textContent = `${creditText(segment.value)} ${unit} · ` +
    (segment.count ? `${segment.count} 个积分包` : '明细未列明');
  tooltip.replaceChildren(title, sum);
  if (segment.hint) {
    const hint = document.createElement('span'); hint.className = 'ring-tip-hint';
    hint.textContent = segment.hint; tooltip.append(hint);
  }
  tooltip.hidden = false;
  const bounds = panel.getBoundingClientRect(), box = target.getBoundingClientRect();
  const legend = panel.querySelector('.ring-legend');
  const anchor = legend ? legend.getBoundingClientRect().right : box.right;
  tooltip.style.left = Math.max(4, Math.min(anchor - bounds.left + 8,
    bounds.width - tooltip.offsetWidth - 4)) + 'px';
  tooltip.style.top = Math.max(0, Math.min(box.top - bounds.top,
    bounds.height - tooltip.offsetHeight)) + 'px';
}

function ringBody(buddy, ring, unit) {
  const panel = document.createElement('section'); panel.className = 'quota-buddy';
  const state = buddyState(buddy);
  const [statusLabel, statusClass] = BUDDY_STATUS[state];
  const status = document.createElement('span'); status.className = `quota-state ${statusClass}`;
  status.textContent = statusLabel;
  const shared = (state === 'known' || state === 'stale') && buddy?.source?.kind === 'account';
  const head = document.createElement('div'); head.className = 'quota-side-head buddy-head';
  const titleGroup = document.createElement('div'); titleGroup.className = 'buddy-title';
  const title = document.createElement('span'); title.className = 'quota-side-title'; title.textContent = 'WorkBuddy';
  titleGroup.append(title);
  if (buddy?.plan && state !== 'error' && state !== 'unknown') {
    const plan = document.createElement('span'); plan.className = 'peer-plan'; plan.textContent = buddy.plan;
    titleGroup.append(plan);
  }
  if (shared) {
    const caption = document.createElement('span'); caption.className = 'buddy-shared';
    caption.textContent = '同账号与 CodeBuddy 共用'; titleGroup.append(caption);
  }
  head.append(titleGroup, status); panel.append(head);

  if (!buddy || state === 'error' || state === 'unknown') {
    const message = document.createElement('p'); message.className = 'buddy-message';
    message.textContent = state === 'error' ? '读取失败；旧额度已清除。' : '额度未知；不显示估算值。';
    panel.append(message);
    const detail = document.createElement('p'); detail.className = 'buddy-meta';
    const parts = [];
    if (buddy?.error) parts.push(buddy.error);
    else if (state === 'unknown') parts.push('等待下一次成功读取');
    if (buddy?.lastAttempt != null) parts.push(`上次尝试 ${time(buddy.lastAttempt, true)}`);
    if (buddy?.source?.label) parts.push(buddy.source.label);
    detail.textContent = parts.join(' · ');
    panel.append(detail);
    return panel;
  }

  if (ring.state === 'ok') {
    const body = document.createElement('div'); body.className = 'buddy-ring-body';
    const wrap = document.createElement('div'); wrap.className = 'ring-wrap';
    wrap.append(ringSvg(ring, buddy.unit || 'credit', panel));
    const center = document.createElement('div'); center.className = 'ring-center';
    const value = document.createElement('strong'); value.textContent = creditText(buddy.remaining);
    const unitLabel = document.createElement('em'); unitLabel.textContent = buddy.unit || 'credit';
    const caption = document.createElement('small'); caption.textContent = '剩余';
    center.append(value, unitLabel, caption); wrap.append(center);
    const legend = document.createElement('div'); legend.className = 'ring-legend';
    ring.segments.forEach((segment, index) => {
      const item = document.createElement('div'); item.className = 'ring-legend-item';
      item.title = ringSegmentText(segment, buddy.unit || 'credit');
      const dot = document.createElement('i');
      dot.style.background = RING_PALETTE[index % RING_PALETTE.length];
      const label = document.createElement('span'); label.textContent = segment.label;
      const amount = document.createElement('b'); amount.textContent = full(segment.value);
      item.append(dot, label, amount); legend.append(item);
    });
    body.append(wrap, legend); panel.append(body);
  } else if (ring.state === 'zero') {
    const body = document.createElement('div'); body.className = 'buddy-ring-body';
    const wrap = document.createElement('div'); wrap.className = 'ring-wrap';
    const svg = svgElement('svg', { viewBox: '0 0 112 112', class: 'ring-svg', role: 'img',
      'aria-label': 'WorkBuddy 剩余额度为 0' });
    svg.append(svgElement('circle', { cx: 56, cy: 56, r: 43, fill: 'none', stroke: '#edf1f8', 'stroke-width': 13 }));
    const center = document.createElement('div'); center.className = 'ring-center';
    const value = document.createElement('strong'); value.textContent = '0';
    const unitLabel = document.createElement('em'); unitLabel.textContent = buddy.unit || 'credit';
    const caption = document.createElement('small'); caption.textContent = '剩余';
    center.append(value, unitLabel, caption); wrap.append(svg, center);
    const message = document.createElement('p'); message.className = 'ring-zero-note';
    message.textContent = '剩余积分为 0（已知）';
    body.append(wrap, message); panel.append(body);
  } else {
    const balance = document.createElement('p'); balance.className = 'buddy-balance';
    balance.textContent = `剩余 ${creditText(buddy.remaining)} ${buddy.unit || 'credit'}`;
    panel.append(balance);
    const message = document.createElement('p'); message.className = 'buddy-message ring-unavailable';
    message.textContent = ring.reason === 'packages-error'
      ? '到期分布暂不可用：积分包明细读取失败。'
      : ring.reason === 'packages-exceed-summary' || ring.reason === 'mismatch'
        ? '积分包明细与汇总不一致，暂不展示到期分布。'
        : ring.reason === 'balance-unknown'
          ? '余额未知，暂不展示到期分布。'
          : '积分包明细不足，暂不展示到期分布。';
    panel.append(message);
    if (buddy.packagesStatus === 'error' && buddy.packagesError) {
      const error = document.createElement('p'); error.className = 'buddy-meta';
      error.textContent = buddy.packagesError; panel.append(error);
    }
  }

  const secondary = document.createElement('p'); secondary.className = 'buddy-summary-line';
  secondary.textContent = secondarySummary(buddy);
  if (secondary.textContent) panel.append(secondary);

  const footer = document.createElement('div'); footer.className = 'buddy-footer';
  const action = document.createElement('button'); action.className = 'peer-action';
  action.textContent = '查看积分包'; action.addEventListener('click', openPackageDialog);
  const source = document.createElement('span'); source.className = 'buddy-source';
  const sourceParts = [];
  if (buddy.source?.label) sourceParts.push(buddy.source.label);
  if (state === 'stale') {
    if (buddy.fetchedAt != null) sourceParts.push(`上次成功 ${time(buddy.fetchedAt, true)}`);
  } else if (buddy.fetchedAt != null) {
    sourceParts.push(`更新 ${time(buddy.fetchedAt, true)}`);
  }
  source.textContent = sourceParts.join(' · ');
  footer.append(action, source); panel.append(footer);
  if (state === 'stale' && buddy.error) {
    const alert = document.createElement('p'); alert.className = 'buddy-meta stale-error';
    alert.textContent = buddy.error; panel.append(alert);
  }
  return panel;
}

let latestBuddy = null;

function packageTime(packageInfo, field, showField, label) {
  const show = packageInfo[showField] === true ||
    (packageInfo[showField] == null && packageInfo[field] != null);
  if (!show) return null;
  const element = document.createElement('span');
  element.className = `buddy-package-time ${field === 'expireAt' ? 'expire' : 'refresh'}`;
  const value = packageInfo[field];
  element.textContent = value == null ? `${label}时间未知` : `${label} ${time(value, true)}`;
  return element;
}

function packageRow(packageInfo, unit) {
  const row = document.createElement('div');
  row.className = 'buddy-package';
  row.dataset.name = packageInfo.name || '';
  row.dataset.expireAt = packageInfo.expireAt == null ? '' : String(packageInfo.expireAt);
  row.dataset.refreshAt = packageInfo.refreshAt == null ? '' : String(packageInfo.refreshAt);
  const main = document.createElement('div'); main.className = 'buddy-package-main';
  const name = document.createElement('span'); name.className = 'buddy-package-name';
  name.textContent = packageInfo.name || '未命名积分包';
  const remaining = document.createElement('strong'); remaining.className = 'buddy-package-remaining';
  remaining.textContent = creditText(packageInfo.remaining);
  const unitLabel = document.createElement('em'); unitLabel.textContent = unit;
  remaining.append(unitLabel);
  main.append(name, remaining);
  const times = document.createElement('div'); times.className = 'buddy-package-times';
  const expire = packageTime(packageInfo, 'expireAt', 'showExpireAt', '到期');
  const refresh = packageTime(packageInfo, 'refreshAt', 'showRefreshAt', '刷新');
  if (expire) times.append(expire);
  if (refresh) times.append(refresh);
  const title = [packageInfo.name || '未命名积分包',
    `剩余 ${creditText(packageInfo.remaining)} ${unit}`];
  if (packageInfo.total != null) title.push(`总量 ${full(packageInfo.total)}`);
  if (packageInfo.used != null) title.push(`已用 ${full(packageInfo.used)}`);
  row.title = title.join('\n');
  row.append(main, times);
  return row;
}

function packageList(buddy) {
  if (buddy.packagesStatus === 'error') {
    const error = document.createElement('p'); error.className = 'package-dialog-error';
    error.textContent = '积分包明细暂不可用' +
      (buddy.packagesError ? `：${buddy.packagesError}` : '。');
    return [error];
  }
  const unit = buddy.unit || 'credit';
  const visibleTime = (item) => {
    if (item.showExpireAt === true && item.expireAt != null) return Number(item.expireAt);
    if (item.showRefreshAt === true && item.refreshAt != null) return Number(item.refreshAt);
    if (item.expireAt != null && item.showExpireAt !== false) return Number(item.expireAt);
    return Infinity;
  };
  const packages = (Array.isArray(buddy.packages) ? buddy.packages : [])
    .filter((item) => item && (item.remaining == null || Number(item.remaining) > 0))
    .sort((a, b) => {
      const first = visibleTime(a), second = visibleTime(b);
      return first - second || (a.refreshAt ?? Infinity) - (b.refreshAt ?? Infinity);
    });
  if (!packages.length) {
    const empty = document.createElement('p'); empty.className = 'package-dialog-empty';
    empty.textContent = '暂无可用的剩余积分包';
    return [empty];
  }
  return packages.map((item) => packageRow(item, unit));
}

function dialogMeta(buddy) {
  const state = buddyState(buddy);
  const parts = [BUDDY_STATUS[state][0], sourceLabel(buddy)];
  if (state === 'stale') {
    if (buddy.fetchedAt != null) parts.push(`上次成功 ${time(buddy.fetchedAt, true)}`);
    if (buddy.error) parts.push(buddy.error);
  } else if (state === 'known' && buddy.fetchedAt != null) {
    parts.push(`更新 ${time(buddy.fetchedAt, true)}`);
  } else if ((state === 'unknown' || state === 'error') && buddy.error) {
    parts.push(buddy.error);
  }
  return parts.join(' · ');
}

function renderPackageDialog(buddy) {
  const summary = document.querySelector('#package-dialog-summary');
  const meta = document.querySelector('#package-dialog-meta');
  const body = document.querySelector('#package-dialog-body');
  if (!summary || !meta || !body) return;
  const state = buddyState(buddy);
  const scroll = body.scrollTop;
  summary.textContent = !buddy ? '额度未知' : state === 'error' ? '读取失败；旧额度已清除。'
    : state === 'unknown' ? '额度未知；不显示估算值。' : creditSummary(buddy);
  meta.textContent = buddy ? dialogMeta(buddy) : '等待账号额度来源';
  body.replaceChildren(...(buddy && state !== 'error' && state !== 'unknown' ? packageList(buddy) : []));
  body.scrollTop = scroll;
}

function openPackageDialog() {
  const dialog = document.querySelector('#package-dialog');
  if (!dialog) return;
  renderPackageDialog(latestBuddy);
  if (!dialog.open) dialog.showModal();
  dialog.querySelector('[data-close]')?.focus();
}

let signature;
export function renderQuota(quota, busy) {
  const next = JSON.stringify([quota, busy, Math.floor(Date.now() / 60000)]);
  if (signature === next) return;
  signature = next;
  const $ = (selector) => document.querySelector(selector);
  $('#quota-refresh').disabled = busy;
  $('#quota-refresh').textContent = busy ? '更新中…' : '更新额度';
  latestBuddy = quota?.buddy ?? null;
  const split = document.createElement('div'); split.className = 'quota-split';
  split.append(codexPanel(quota, busy), ringBody(latestBuddy, buildQuotaRing(latestBuddy), latestBuddy?.unit || 'credit'));
  $('#quota-cards').replaceChildren(split);
  $('#quota-error').textContent = quota?.error || '';
  $('#quota-error').hidden = !quota?.error;
  if ($('#package-dialog')?.open) renderPackageDialog(latestBuddy);
}
