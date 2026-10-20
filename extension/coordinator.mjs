export const ALARM = 'codex-monitor-hourly';
export const QUOTA_ALARM = 'codex-monitor-quota';
export const INTERVAL = 60 * 1000;
export const QUOTA_INTERVAL = 5 * 60 * 1000;
export const REFRESH_MINUTES = [0.5, 1, 5, 15, 60];

export class Coordinator {
  constructor(api, native, now = () => Date.now()) {
    this.api = api;
    this.native = native;
    this.now = now;
    this.state = { busy: false, error: null, status: null, nextAlarm: null, progress: null, quota: null, quotaBusy: false, refreshMinutes: 1, quotaLastAttempt: 0 };
    this.running = null;
    this.quotaRunning = null;
    this.queries = new Map();
    this.ready = null;
    this.scheduling = null;
  }

  async save() {
    await this.api.storage.local.set({ monitorState: this.state });
  }

  setStatus(next) {
    const previous = this.state.status;
    const previousRoots = JSON.stringify(previous?.buddy?.roots ?? null);
    const nextRoots = JSON.stringify(next?.buddy?.roots ?? null);
    if (previous?.home !== next.home || previous?.version !== next.version
        || previousRoots !== nextRoots) this.queries.clear();
    if (previous?.home !== next.home) {
      this.state.quota = next.quota ?? null;
      this.state.quotaLastAttempt = 0;
    }
    else if ((next.quota?.lastAttempt ?? 0) > (this.state.quota?.lastAttempt ?? 0)) this.state.quota = next.quota;
    this.state.status = { ...next, quota: this.state.quota };
  }

  refreshQuota() {
    const home = this.state.status?.home;
    if (this.quotaRunning && this.quotaRunning.home === home) return this.quotaRunning.promise;
    const run = { home };
    run.promise = (async () => {
      this.state.quotaBusy = true;
      this.state.quotaLastAttempt = this.now() / 1000;
      await this.save();
      try {
        const result = await this.native('quota', {});
        if (result?.home === this.state.status?.home && result?.quota) {
          this.state.quota = result.quota;
          this.state.status = { ...this.state.status, quota: result.quota };
        }
        return result;
      } catch (error) {
        if (home === this.state.status?.home) this.state.quota = { ...this.state.quota, error: error.message };
        throw error;
      } finally {
        if (this.quotaRunning === run) {
          this.state.quotaBusy = false;
          this.quotaRunning = null;
          await this.save();
        }
      }
    })();
    this.quotaRunning = run;
    return run.promise;
  }

  refreshQuotaIfDue() {
    const last = Math.max(this.state.quotaLastAttempt || 0, this.state.quota?.lastAttempt || this.state.quota?.fetchedAt || 0);
    if (!last || this.now() - last * 1000 >= QUOTA_INTERVAL) this.refreshQuota().catch(() => {});
  }

  async query(op, params) {
    const key = JSON.stringify([op, this.state.status?.home, this.state.status?.version, params]);
    if (!this.queries.has(key)) {
      const pending = this.native(op, params).then(async (data) => {
        const current = this.state.status;
        if (data.status.home === current?.home && data.status.version < current.version) return this.query(op, params);
        if (data.status.home === current?.home && data.status.version > current.version) {
          this.setStatus(data.status);
          await this.save();
        }
        return data;
      });
      this.queries.set(key, pending);
      pending.catch(() => { if (this.queries.get(key) === pending) this.queries.delete(key); });
      if (this.queries.size > 40) this.queries.delete(this.queries.keys().next().value);
    }
    const data = await this.queries.get(key);
    const current = this.state.status;
    return data.status.home === current?.home && data.status.version === current.version
      ? { ...data, status: current } : data;
  }

  schedule() {
    if (this.scheduling) return this.scheduling;
    this.scheduling = (async () => {
      for (const [name, minutes] of [[ALARM, this.state.refreshMinutes], [QUOTA_ALARM, 5]]) {
        let alarm = await this.api.alarms.get(name);
        if (!alarm || alarm.periodInMinutes !== minutes) {
          await this.api.alarms.create(name, { delayInMinutes: minutes, periodInMinutes: minutes });
          alarm = await this.api.alarms.get(name);
        }
        if (name === ALARM) this.state.nextAlarm = alarm?.scheduledTime ?? null;
      }
    })().finally(() => { this.scheduling = null; });
    return this.scheduling;
  }

  initialize() {
    if (this.ready) return this.ready;
    this.ready = (async () => {
      const stored = await this.api.storage.local.get('monitorState');
      this.state = { ...this.state, ...stored.monitorState, busy: false, progress: null, quotaBusy: false };
      if (!REFRESH_MINUTES.includes(this.state.refreshMinutes)) this.state.refreshMinutes = 1;
      await this.schedule();
      try {
        this.setStatus(await this.native('status', {}));
        this.state.error = null;
        await this.save();
        const last = this.state.status.lastSuccess;
        if (!last || this.state.status.responseBackfillPending || this.state.status.buddy?.responseBackfillPending || this.now() - last * 1000 >= this.state.refreshMinutes * INTERVAL) {
          this.refresh(last ? '恢复补刷' : '首次导入').catch(() => {});
        }
        this.refreshQuotaIfDue();
      } catch (error) {
        this.state.error = error.message;
        await this.save();
      }
    })();
    return this.ready;
  }

  refresh(reason = '手动刷新') {
    if (this.running) return this.running;
    this.running = (async () => {
      this.state.busy = true;
      this.state.error = null;
      this.state.progress = null;
      this.state.reason = reason;
      await this.save();
      try {
        const result = await this.native('refresh', {}, (progress) => {
          this.state.progress = progress;
          this.save().catch(() => {});
        });
        this.setStatus(result.status);
        const warnings = [...(result.import?.warnings || []),
          ...(result.buddyImport?.warnings || [])];
        this.state.error = warnings.length ? warnings.join('；') : null;
        return result;
      } catch (error) {
        this.state.error = error.message;
        throw error;
      } finally {
        this.state.busy = false;
        this.state.progress = null;
        this.running = null;
        await this.schedule();
        await this.save();
      }
    })();
    return this.running;
  }

  async dispatch(message) {
    await this.initialize();
    switch (message.op) {
      case 'state': return this.state;
      case 'refresh':
        this.refreshQuota().catch(() => {});
        return this.refresh();
      case 'visit':
        this.refreshQuotaIfDue();
        return this.refresh('查看页面');
      case 'quota': return this.refreshQuota();
      case 'details':
      case 'stats': return this.query(message.op, message.params ?? {});
      case 'configure': {
        const input = message.params ?? {};
        const minutes = Number(input.refreshMinutes ?? this.state.refreshMinutes);
        if (!REFRESH_MINUTES.includes(minutes)) throw new Error('请选择支持的自动刷新间隔。');
        if (this.running) await this.running.catch(() => {});
        const current = this.state.status;
        const home = typeof input.home === 'string' ? input.home.trim() : null;
        if (home === '') throw new Error('请选择包含 sessions 子目录的 Codex 目录。');
        const payload = {};
        if (home && home !== current?.home) payload.home = home;
        for (const [key, root] of [
          ['workbuddy', current?.buddy?.roots?.workbuddy],
          ['codebuddy', current?.buddy?.roots?.codebuddy],
          ['bridge', current?.buddy?.roots?.bridge],
        ]) {
          if (typeof input[key] !== 'string') continue;
          const value = input[key].trim();
          if (value !== (root || '')) payload[key] = value;
        }
        if (Object.keys(payload).length) {
          await this.native('configure', payload);
          this.setStatus(await this.native('status', {}));
        }
        this.state.refreshMinutes = minutes;
        await this.schedule();
        await this.save();
        this.refreshQuotaIfDue();
        return this.refresh('保存设置');
      }
      default: throw new Error('不支持的操作。');
    }
  }

  async alarm(alarm) {
    if (alarm.name !== ALARM && alarm.name !== QUOTA_ALARM) return;
    await this.initialize();
    if (alarm.name === QUOTA_ALARM) return this.refreshQuota();
    return this.refresh('自动刷新');
  }
}
