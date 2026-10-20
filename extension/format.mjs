export const DASH = '—';
export function tokens(value) {
  if (value == null) return DASH;
  let n = value, index = 0;
  const units = ['', 'K', 'M', 'B', 'T'];
  while (n >= 1000 && index < units.length - 1) { n /= 1000; index++; }
  if (Number(n.toFixed(2)) >= 1000 && index < units.length - 1) { n /= 1000; index++; }
  return index ? n.toFixed(2).replace(/\.00$/, '') + units[index] : String(n);
}
export const full = (value) => value == null ? DASH : Number(value).toLocaleString('en-US');
export const percent = (value) => value == null ? DASH : (value * 100).toFixed(1) + '%';
export const rate = (value) => value == null ? DASH : value.toFixed(1);
export const seconds = (value) => value == null ? DASH : value > 0 && value < .01 ? '<0.01' : value.toFixed(value < 10 ? 2 : 1);
export const USD_CNY = 6.8;
export const money = (value, currency = 'USD') => value == null ? DASH
  : '~' + (currency === 'CNY' ? '¥' : '$') + (value * (currency === 'CNY' ? USD_CNY : 1)).toFixed(4);
export const credit = (value) => value == null ? DASH : String(Number(value.toFixed(6)));
export const mode = (value) => ({ standard: 'Standard', fast: 'Fast' })[value] ?? DASH;
export const sourceLabel = (value) => ({
  codex: 'Codex', 'workbuddy-desktop': 'WorkBuddy 桌面端',
  'codebuddy-cli': 'CodeBuddy CLI', all: '全部来源',
})[value] ?? DASH;
export function sessionLabel(summary) {
  if (!summary) return DASH;
  if (summary.confirmedResumeCount) {
    return `续接已证实 · ${summary.runCount} 次运行` +
      (summary.failedCount || summary.mismatchCount || summary.unconfirmedCount ? ' · 含异常/未证实'
        : summary.pendingCount ? ' · 含执行中' : '');
  }
  if (summary.matchedResumeCount) return `续接编号一致 · ${summary.runCount} 次运行 · 未成功完成`;
  const label = {
    resumed: '续接已证实', new: '新会话', mismatch: '续接不匹配',
    failed: '关联运行失败', unconfirmed: '续接未证实', pending: '运行中/待定', unknown: '未知',
  }[summary.outcome] ?? '未知';
  return summary.runCount ? `${label} · ${summary.runCount} 次运行` : label;
}
export const day = (date) => [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
export function range(days, now = new Date()) {
  const start = new Date(now);
  start.setDate(start.getDate() - days + 1);
  return { from: day(start), to: day(now) };
}
export function time(value, short = false) {
  if (!value) return DASH;
  const d = new Date(value * 1000);
  const clock = d.toLocaleTimeString('zh-CN', { hour12: false });
  return (short ? day(d).slice(5) : day(d)) + ' ' + clock;
}
