# 开发与测试

本文只覆盖当前仓库实际使用的流程：Windows、Python 3.12、Node，以及可选的 Playwright Chromium。命令均在仓库根目录的 PowerShell 中执行。

## 环境

| 用途 | 要求 |
|---|---|
| 运行本地读取器 / Python 测试 | Python 3.12 |
| 打包 | Windows + .NET Framework 4 编译器（`C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`） |
| Node 测试 | Node 24（开发时版本） |
| 界面测试 | Playwright + Chromium，仅开发依赖 |

运行时代码只使用 Python 标准库；没有 `requirements.txt`，也不需要 `npm install`。

## 目录职责

- `extension/`：Chrome 扩展。`background.mjs` 负责消息与定时器，`coordinator.mjs` 协调刷新，`dashboard.*` 渲染界面。
- `native/codex_monitor/`：Python 读取器。`host.py` 处理 Native Messaging 帧，`service.py` 提供操作，`store.py`/`buddy_store.py` 维护 SQLite 索引，`quota.py`/`buddy_quota.py` 读取账号额度，`pricing.py` 计算参考价。
- `launcher/Launcher.cs`：无控制台的启动器，负责转发标准输入输出。
- `scripts/`：构建与安装脚本。
- `tests/`：Python、Node 与界面测试。

## 运行测试

Python 测试需要把 `native` 加入 `PYTHONPATH`：

```powershell
$env:PYTHONPATH = (Join-Path $PWD 'native')
python -m unittest discover -s tests -p 'test_*.py' -v
```

该变量只在当前 PowerShell 会话生效；新开窗口需要重新设置。

Node 测试使用 `node:test`，无需额外依赖：

```powershell
node --test tests/background.test.mjs tests/buddy.test.mjs tests/quota-ring.test.mjs tests/usage-chart.test.mjs
```

也可以只跑单个文件：

```powershell
node --test tests\background.test.mjs
```

### 界面测试（可选）

`tests/ui_v032.py` 是当前唯一的界面测试脚本。它用合成状态在本地 HTTP 服务上加载 `extension/dashboard.html`，并在 Chromium 中模拟 `chrome.*` API，不接触真实扩展配置、日志或账号。

```powershell
python -m pip install playwright
python -m playwright install chromium
python .\tests\ui_v032.py
```

脚本以无头模式运行，截图写入 `artifacts\screenshots`。

## 构建

```powershell
python .\scripts\build.py
```

脚本会：

1. 复制当前 Python 3.12 运行时（`python.exe`、`python3xx.dll`、`python3.dll`、`vcruntime*.dll`、`DLLs`、`stdlib.zip`）到 `dist\AgentUsageDesk\runtime`。
2. 复制 `extension`、`native`、`docs`、`README.md`、`LICENSE`、`THIRD-PARTY.md` 与安装脚本。
3. 用 `csc.exe` 编译 `launcher\Launcher.cs` 为 `CodexMonitorHost.exe`。
4. 生成 `extension-id.txt`，并打包为 `dist\AgentUsageDesk-<版本>-windows.zip`。

ZIP 的顶层目录为 `AgentUsageDesk`。内部的 `CodexMonitorHost.exe`、扩展目录与安装目录名保持不变。

安装产物：

```powershell
cd .\dist\AgentUsageDesk
pwsh -NoProfile -File .\install.ps1
```

安装把文件复制到 `%LOCALAPPDATA%\CodexMonitor\app`，注册 `com.codexmonitor.reader`，并打印扩展 ID 与加载路径。卸载使用同一目录下的 `uninstall.ps1`。

## 保持不变的标识

以下名称被已有安装、注册表项与文档引用，**不要为了品牌调整而重命名**：

| 标识 | 值 |
|---|---|
| 内部数据/安装目录 | `%LOCALAPPDATA%\CodexMonitor\app` |
| Native Messaging 主机名 | `com.codexmonitor.reader` |
| 启动器可执行文件 | `CodexMonitorHost.exe` |
| Python 包 | `codex_monitor` |
| 索引文件 | `index-<哈希>.sqlite3`、`buddy-index.sqlite3` |

对外品牌是 Agent Usage Desk；上述标识属于兼容层。

## 关于扩展 `key`

`extension/manifest.json` 中的 `key` 是 Base64 编码的**公钥**，不是私钥或秘密。Chrome 用它的 SHA-256 前 16 字节推导扩展 ID，映射规则是把每个十六进制字符对应到 `a`–`p`：

```text
ID = f(SHA256(DER 公钥))  // 取前 32 个十六进制字符，逐位映射为 a-p
```

`build.py` 与 `install.ps1` 都按同一规则计算 ID，后者写入 Native Messaging 清单的 `allowed_origins`。因此：

- 公钥可以公开提交，任何加载此扩展的人都会得到相同 ID；
- 它不能用于给 CRX 签名，也不授予任何账号权限；
- 更换 `key` 会改变扩展 ID，导致既有注册项失配，请勿随意修改。

## 数据边界

- 测试使用 `tempfile` 临时目录与合成状态，不需要真实账号或真实日志。
- 界面测试只启动 `127.0.0.1` 上的静态文件服务。
- 不要在测试或示例中写入真实用户名路径、会话编号或额度快照。

## 公开版准备验证（2026-10-20）

在 Windows 10 本机，99 项 Python 测试、27 项 Node 测试、合成数据 Chromium 界面检查和 Windows 打包通过。安装/卸载脚本通过 PowerShell 语法检查，本轮未实际安装或修改注册表，未查询真实账号。统计核心与现有 0.3.2 一致；修改限于公开文档、显示名称和安装包许可文件。

首次源码上传后，[GitHub Actions 的 Windows 检查](https://github.com/JSbiu/agent-usage-desk/actions/runs/39362391105) 已通过 Python 与 Node 测试。第三方二进制的完整许可核对尚未完成，详见 [第三方说明](third-party.md)；本轮仅公开源码，未上传安装包。

## 0.3.3 数据健康度验证（2026-10-20）

0.3.3 新增 `status.health` 以及每个来源的 `skipped` / `invalid` 计数，用于暴露解析失败后被跳过的日志行。索引结构升级到 `user_version=4`：`files` 表新增 `skipped` 列，迁移时从既有 state JSON 回填历史值；列已存在时跳过 `ALTER TABLE`，避免升级中断后重复执行报「duplicate column」。

验证范围：102 项 Python 测试（含 `tests/test_health.py` 的 3 项新增用例）、27 项 Node 测试、`extension/dashboard.mjs` 语法检查。

本轮执行的运行时为 WSL Ubuntu 22.04 的 Python 3.10.12 与 Windows 的 Node 22.23.3，**未在 Windows 10 + Python 3.12 + Node 24 的发布环境复跑**；Playwright 界面测试与 Windows 打包本轮未执行。归档截图与界面实测留待发版前按 [release.md](release.md) 的核对项补齐。

## 0.3.4 额度可见性（2026-10-20）

- WorkBuddy 5.6 起把 `auth.accessToken` 改为 at-rest 加密信封（`{"$wbEncrypted":1,"envelope":...}`）。读取器现在以 `ELECTRON_RUN_AS_NODE=1` 调用 WorkBuddy 自带 Electron 运行时取回构建期密钥，在内存中完成 AES-256-GCM 解密；`token_helper.js` 随 native 包分发。找不到客户端或解密失败时分别给出「未找到 WorkBuddy 主程序」与「登录令牌解密失败」提示，不再建议重复登录。
- `quota.executable()` 支持桌面应用把二进制放在 `bin/<构建哈希>/codex.exe` 的布局，按写入时间取最新一个；仍优先使用 PATH 与旧版 `bin/codex.exe`。
- `coordinator.query()` 成功后清除 storage 中残留的旧错误，避免一次失败的初始化让提示条长期驻留。

验证：105 项 Python 测试（含新增 `tests/test_encrypted_credential.py` 3 项）、Node 测试与 `coordinator.mjs` 语法检查通过。本机实测解密耗时 0.2 s、令牌长度 1536；`codex app-server` 实时读回 plus 套餐的 5 小时与周窗口用量。仍未在 Windows 10 + Python 3.12 发布环境复跑打包与界面测试。

## 0.3.5 项目维度归因（2026-10-20）

- 解析层从 `session_meta.payload.cwd`、`thread_settings.cwd`（Codex）与每行的 `cwd`（WorkBuddy / CodeBuddy CLI）提取工作目录，经 `core.project_key()` 归一化为分组键：反斜杠转正斜杠、去掉尾部斜杠、盘符大写、超长或含换行的值直接丢弃。
- 索引结构升级到 `user_version=5`：`requests` 表新增 `project` 列。项目维度只能从原始日志取，迁移时清空 `files` 偏移触发一次全量重建；`buddy_store` 的同类迁移放在版本判断之外，保证已有库也会补列。
- 新增 `projects` 操作，直接对 `requests` 聚合（不经过 `daily` 预聚合），按成本降序返回记录数、token、缓存与成本，并列出贡献来源。
- 前端新增「项目」标签页；项目名取路径末两段显示，完整路径放在 tooltip 里。

验证：111 项 Python 测试（含新增 `tests/test_projects.py` 6 项）、Node 测试与 `dashboard.mjs` / `coordinator.mjs` 语法检查通过。本机实测 43 个项目分组，Codex 2573 条与 WorkBuddy 5365 条全部完成归因；同一路径的 `handbook` 已合并为一条。仍未在 Windows 10 + Python 3.12 发布环境复跑打包与界面测试。
