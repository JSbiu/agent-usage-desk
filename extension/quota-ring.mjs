export const RING_PALETTE = ['#5279e1', '#33a59e', '#dfad64', '#9a7ddc', '#9babbc', '#5aa9d6', '#c5905d', '#d582a6'];

const DAY_MS = 86400000;
const MAX_INDIVIDUAL_DAYS = 3;
const EPSILON = 0.000001;

const pad = (value) => String(value).padStart(2, '0');
const dayStart = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
const dateFull = (milliseconds) => {
  const date = new Date(milliseconds);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};
const dateShort = (milliseconds) => {
  const date = new Date(milliseconds);
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};
const dayLabel = (difference) => {
  if (difference === 0) return '今天';
  if (difference === 1) return '明天';
  if (difference === 2) return '后天';
  return null;
};

function sumRemaining(packages) {
  return packages.reduce((sum, item) => sum + Number(item.remaining), 0);
}

function refreshRange(packages) {
  const values = packages.map((item) => item.refreshAt).filter((value) => value != null);
  if (!values.length) return '';
  const first = Math.min(...values) * 1000, last = Math.max(...values) * 1000;
  return `${dateFull(first)}${first === last ? '' : ' 至 ' + dateFull(last)}`;
}

/**
 * Group available package credits by expiry day. Used/total/groups never enter
 * the sum. When the package details cover less than the summary balance, the
 * uncovered remainder is shown as an explicit "其他余额" segment.
 */
export function buildQuotaRing(buddy, now = new Date()) {
  const empty = (state, reason, extra = {}) => ({
    state, reason, total: buddy?.remaining ?? null, packageTotal: null,
    residual: null, segments: [], ...extra,
  });
  if (!buddy || buddy.status === 'unknown' || buddy.status === 'error') {
    return empty('unavailable', 'status');
  }
  if (buddy.packagesStatus === 'error') return empty('unavailable', 'packages-error');
  const remaining = buddy.remaining;
  if (remaining == null) return empty('unavailable', 'balance-unknown');
  if (remaining === 0) return { state: 'zero', total: 0, packageTotal: 0, residual: 0, segments: [] };

  const packages = (Array.isArray(buddy.packages) ? buddy.packages : [])
    .filter((item) => item && item.remaining != null && Number(item.remaining) > 0);
  if (!packages.length) return empty('empty', 'no-packages');

  const packageTotal = sumRemaining(packages);
  if (packageTotal > remaining + EPSILON) return empty('mismatch', 'packages-exceed-summary', { packageTotal });

  const nowMs = now.getTime();
  const today = dayStart(now);
  const overdue = [];
  const refresh = [];
  const unknown = [];
  const days = new Map();
  for (const item of packages) {
    if (item.showExpireAt === true && item.expireAt != null) {
      const expires = Number(item.expireAt) * 1000;
      if (expires < nowMs) overdue.push(item);
      else {
        const day = dayStart(new Date(expires));
        if (!days.has(day)) days.set(day, []);
        days.get(day).push(item);
      }
    } else if (item.showExpireAt !== true && item.showRefreshAt === true) {
      refresh.push(item);
    } else {
      unknown.push(item);
    }
  }

  const segments = [];
  const add = (key, label, kind, items, extra = {}) => {
    if (!items.length) return;
    const value = sumRemaining(items);
    if (!(value > 0)) return;
    segments.push({ key, label, kind, value, count: items.length, ...extra });
  };
  add('overdue', '已过期', 'overdue', overdue, { hint: '到期时间已过' });
  const orderedDays = [...days.entries()].sort((a, b) => a[0] - b[0]);
  orderedDays.slice(0, MAX_INDIVIDUAL_DAYS).forEach(([day, items]) => {
    const difference = Math.round((day - today) / DAY_MS);
    add(`day:${day}`, dayLabel(difference) || dateShort(day), 'day', items,
      { date: dateFull(day), day, difference });
  });
  const later = orderedDays.slice(MAX_INDIVIDUAL_DAYS).flatMap(([, items]) => items);
  if (later.length) {
    const laterDays = orderedDays.slice(MAX_INDIVIDUAL_DAYS).map(([day]) => day);
    add('later', '较晚到期', 'later', later, {
      range: `${dateFull(Math.min(...laterDays))}${laterDays.length === 1 ? '' : ' 至 ' + dateFull(Math.max(...laterDays))}`,
    });
  }
  add('refresh', '周期刷新', 'refresh', refresh, {
    hint: '按刷新周期，不是到期', range: refreshRange(refresh),
  });
  add('unknown', '时间未知', 'unknown', unknown, { hint: '没有可用的到期时间' });

  const remainder = remaining - packageTotal;
  if (remainder > EPSILON) {
    segments.push({ key: 'residual', label: '其他余额', kind: 'residual', value: remainder,
      count: 0, hint: '积分包明细未覆盖的余额，不推断到期时间' });
  } else if (remainder < 0 && Math.abs(remainder) <= EPSILON && segments.length) {
    segments[segments.length - 1].value += remainder;
  }
  return {
    state: 'ok', total: remaining, packageTotal, residual: remainder > EPSILON ? remainder : 0,
    segments,
  };
}

export function ringSegmentText(segment, unit = 'credit') {
  const parts = [segment.label];
  if (segment.date) parts.push(segment.date);
  if (segment.range) parts.push(segment.range);
  if (segment.hint) parts.push(segment.hint);
  parts.push(`${Number(segment.value).toLocaleString('en-US')} ${unit}`);
  parts.push(segment.count ? `${segment.count} 个积分包` : '明细未列明');
  return parts.join(' · ');
}
