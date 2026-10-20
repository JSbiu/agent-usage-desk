import { Coordinator } from './coordinator.mjs';

const HOST = 'com.codexmonitor.reader';

function nativeConnection(idleMs) {
  let port = null, idle = null;
  const pending = new Map();
  function close() { const previous = port; port = null; previous?.disconnect(); }
  function finish(id, error, data) {
    const request = pending.get(id);
    if (!request) return;
    pending.delete(id); clearTimeout(request.timeout);
    if (error) request.reject(error); else request.resolve(data);
    if (!pending.size && port) idle = setTimeout(close, idleMs);
  }
  return (op, params, progress) => new Promise((resolve, reject) => {
    clearTimeout(idle);
    const id = crypto.randomUUID();
    const timeout = setTimeout(() => {
      const error = new Error('读取器响应超时，请重试刷新。');
      close(); for (const key of pending.keys()) finish(key, error);
    }, 10 * 60 * 1000);
    pending.set(id, { resolve, reject, progress, timeout });
    try {
      if (!port) {
        const connected = chrome.runtime.connectNative(HOST);
        port = connected;
        connected.onMessage.addListener((message) => {
          if (message.type === 'progress') { pending.get(message.id)?.progress?.(message.progress); return; }
          finish(message.id, message.ok ? null : new Error(message.error || '读取器返回错误。'), message.data);
        });
        connected.onDisconnect.addListener(() => {
          const reason = chrome.runtime.lastError?.message;
          if (port !== connected) return;
          port = null;
          const error = new Error(reason ? '本地读取器未连接：' + reason : '本地读取器意外断开。');
          for (const key of pending.keys()) finish(key, error);
        });
      }
      port.postMessage({ id, op, params });
    } catch (error) { finish(id, error); }
  });
}

// Reuse a short local-query burst. Account network waits use a separate process.
const localRequest = nativeConnection(1000), quotaRequest = nativeConnection(0);
const nativeRequest = (op, params, progress) => (op === 'quota' ? quotaRequest : localRequest)(op, params, progress);

const coordinator = new Coordinator(chrome, nativeRequest);
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  coordinator.dispatch(message).then(
    (data) => reply({ ok: true, data }),
    (error) => reply({ ok: false, error: error.message }),
  );
  return true;
});
chrome.alarms.onAlarm.addListener((alarm) => coordinator.alarm(alarm).catch(() => {}));
chrome.runtime.onStartup.addListener(() => coordinator.initialize());
chrome.runtime.onInstalled.addListener(() => coordinator.initialize());
chrome.action.onClicked.addListener(async () => {
  const { dashboardTab } = await chrome.storage.local.get('dashboardTab');
  if (dashboardTab) {
    try {
      const previous = await chrome.tabs.get(dashboardTab);
      if (previous.url === chrome.runtime.getURL('dashboard.html')) {
        await chrome.tabs.update(dashboardTab, { active: true }); return;
      }
    } catch {}
  }
  const tab = await chrome.tabs.create({ url: chrome.runtime.getURL('dashboard.html') });
  await chrome.storage.local.set({ dashboardTab: tab.id });
});
coordinator.initialize().catch(() => {});
