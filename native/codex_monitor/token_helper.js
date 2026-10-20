#!/usr/bin/env node
/*
 * token_helper.js — 从 WorkBuddy 加密登录态中解出 accessToken
 *
 * 背景（2026-09-30 实测）：
 *   WorkBuddy 客户端 5.6.x 起，%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\
 *   workbuddy-desktop.info 里的 auth.accessToken / refreshToken 不再是明文，
 *   而是 at-rest 加密信封：
 *       {"$wbEncrypted":1,"envelope":"<base64 of {suite,keyId,nonce,authTag,ciphertext}>"}
 *
 *   加密算法（来自客户端 app.asar 内 @repo/at-rest-crypto）：
 *       key    = SHA256(atRestSecretKey)              // atRestSecretKey 是 44 字符 base64
 *       keyId  = SHA256(key).hex[:16]
 *       cipher = AES-256-GCM，AAD = buildAuthenticatedContextAad(keyId, suite, {framing:"field"})
 *       AAD 拼接 = "WB-AAD\0" | 0x01 | lenPref("WBEV1") | lenPref("sym-v1")
 *                  | uint32(suite) | lenPref(keyId) | 0x02 | 0x00 | 0x00
 *       （lenPref = uint32 长度 + UTF-8 字节；0x02 = FRAMING_CODE.field）
 *
 *   atRestSecretKey 是构建期常量，不在磁盘上，也没有明文副本；只能通过客户端自带的
 *   Electron 原生绑定取：process._linkedBinding("electron_browser_workbuddy_storage").loggerGet()
 *   该绑定在 ELECTRON_RUN_AS_NODE=1 模式下同样可用，因此无需启动 App、无需 UI：
 *
 *       ELECTRON_RUN_AS_NODE=1 "<WorkBuddy.exe>" token_helper.js "<auth-info 路径>"
 *
 * 约定：
 *   - 成功：stdout 只输出 token 本身（无换行、无其他内容），退出码 0
 *   - 失败：stderr 输出 "TOKEN_HELPER_ERROR <原因>"，退出码非 0
 *   - 任何情况下都不打印密钥、完整 token
 */

'use strict';

const crypto = require('crypto');
const fs = require('fs');

function fail(msg, code) {
  process.stderr.write('TOKEN_HELPER_ERROR ' + msg + '\n');
  process.exit(code || 1);
}

function u32(n) {
  const b = Buffer.allocUnsafe(4);
  b.writeUInt32BE(n >>> 0);
  return b;
}
function lenPref(s) {
  const b = Buffer.from(s, 'utf8');
  return Buffer.concat([u32(b.length), b]);
}

const AAD_DOMAIN = Buffer.from('WB-AAD\0', 'ascii');
const FRAMING_CODE = { file: 1, field: 2, record: 3, stream: 4 };
const STANDARD_FORMAT_ID = { file: 'WBEF1', field: 'WBEV1', record: 'WBER1', stream: 'WBES1' };

function buildAad(keyId, suite, framing) {
  return Buffer.concat([
    AAD_DOMAIN,
    Buffer.from([1]),
    lenPref(STANDARD_FORMAT_ID[framing]),
    lenPref('sym-v1'),
    u32(suite),
    lenPref(keyId),
    Buffer.from([FRAMING_CODE[framing]]),
    Buffer.from([0]),   // encodeOptionalUint64(undefined)
    Buffer.from([0]),   // final 缺省
  ]);
}

// ---------- 1. 取构建期密钥 ----------
let binding;
try {
  binding = process._linkedBinding('electron_browser_workbuddy_storage');
} catch (e) {
  fail('原生绑定不可用（请确认用 WorkBuddy.exe 且 ELECTRON_RUN_AS_NODE=1 启动）: ' + e.message, 2);
}
if (!binding || typeof binding.loggerGet !== 'function') {
  fail('原生绑定结构不符预期（客户端版本可能已变更）', 2);
}

let rawPayload;
try {
  rawPayload = binding.loggerGet();
} catch (e) {
  fail('loggerGet() 调用失败: ' + e.message, 2);
}

let payload;
try {
  payload = JSON.parse(rawPayload);
} catch (e) {
  fail('密钥 payload 不是合法 JSON', 3);
}
if (!payload || payload.version !== 1 || typeof payload.atRestSecretKey !== 'string') {
  fail('密钥 payload 结构与预期不符', 3);
}

const key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
const keyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);

// ---------- 2. 读登录态 ----------
const authPath = process.argv[2];
if (!authPath) fail('缺少登录态文件路径参数', 4);

let info;
try {
  info = JSON.parse(fs.readFileSync(authPath, 'utf8'));
} catch (e) {
  fail('读取登录态失败: ' + e.message, 5);
}
const auth = (info && info.auth) || {};

// 明文（旧版本客户端）直接返回
for (const name of ['accessToken', 'access_token']) {
  const v = auth[name];
  if (typeof v === 'string' && v) {
    process.stdout.write(v, () => process.exit(0));
    return;
  }
}

// ---------- 3. 解加密信封 ----------
const wrapper = auth.accessToken || auth.access_token;
if (!wrapper || typeof wrapper !== 'object' || typeof wrapper.envelope !== 'string') {
  fail('登录态中没有可用的 accessToken', 6);
}

let envelope;
try {
  envelope = JSON.parse(Buffer.from(wrapper.envelope, 'base64').toString('utf8'));
} catch (e) {
  fail('信封解码失败: ' + e.message, 7);
}
if (envelope.suite !== 1) fail('不支持的加密套件 suite=' + envelope.suite, 8);
if (envelope.keyId !== keyId) {
  fail('keyId 不匹配（信封=' + envelope.keyId + ' 本地派生=' + keyId + '）', 8);
}

let token;
try {
  const nonce = Buffer.from(envelope.nonce, 'base64');
  const authTag = Buffer.from(envelope.authTag, 'base64');
  const ciphertext = Buffer.from(envelope.ciphertext, 'base64');
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, nonce);
  decipher.setAAD(buildAad(envelope.keyId, envelope.suite, 'field'));
  decipher.setAuthTag(authTag);
  token = Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
} catch (e) {
  fail('GCM 解密失败: ' + e.message, 9);
}

if (!token) fail('解密结果为空', 9);
process.stdout.write(token, () => process.exit(0));
