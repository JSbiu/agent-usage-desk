# AGENTS.md

面向在本仓库工作的维护者与自动化代理。项目对外名称为 **Agent Usage Desk**，版本 0.3.3，MIT 许可，仅在 Windows 上开发和验证。

## 项目结构

| 路径 | 职责 |
|---|---|
| `extension/` | Chrome 扩展：manifest、后台协调器、仪表盘 |
| `native/codex_monitor/` | Python 本地读取器：日志解析、SQLite 索引、额度、计价 |
| `launcher/Launcher.cs` | Native Messaging 启动器 |
| `scripts/` | `build.py`、`install.ps1`、`uninstall.ps1` |
| `tests/` | Python/Node 单元测试与 `ui_v032.py` 界面测试 |
| `docs/` | 隐私、开发、发布核对、第三方与价格说明 |

常用命令见 [docs/development.md](docs/development.md)，发版前的版本同步位置与核对项见 [docs/release.md](docs/release.md)。仓库没有 npm 依赖；运行时代码只用 Python 标准库。

## 兼容性约束

以下标识被已有安装与注册项引用，除非同时提供迁移方案，否则不要修改：

- 安装/数据目录 `%LOCALAPPDATA%\CodexMonitor`（应用在 `app` 子目录）；
- Native Messaging 主机名 `com.codexmonitor.reader`；
- 启动器 `CodexMonitorHost.exe`；
- Python 包名 `codex_monitor`；
- `manifest.json` 中的 `key`、`version` 与权限集合；
- 索引文件名 `index-<哈希>.sqlite3`、`buddy-index.sqlite3`。

`extension/manifest.json` 的 `key` 是公钥，用于固定扩展 ID，不是秘密。修改它会让已安装用户失配。

品牌调整只改用户可见文案（扩展名称、标题、安装提示、主机 description）；内部标识保持不变。

## 本地数据保护

- 原始日志只读；不得新增写入、移动或删除用户日志的行为。
- 索引不得保存聊天正文、提示词、回复或工具输出。新增字段前先确认它只是统计元数据。
- 不得把真实用户名路径、账号快照、会话编号、额度数据或日志片段写入仓库、测试、文档、截图或提交信息。测试一律使用临时目录与合成数据。
- 不提交 `%LOCALAPPDATA%\CodexMonitor` 的内容、`dist/`、`artifacts/screenshots/` 或任何本机产物。
- 不读取真实凭据，不在测试中调用真实账号接口。

## 网络行为

现有网络行为只有账号额度查询：

1. 通过本机 `codex app-server` 的 `account/rateLimits/read` 读取 Codex 订阅额度；
2. 只读使用 WorkBuddy 登录凭据，仅访问 `www.workbuddy.cn` 上三个固定计费地址，不刷新令牌。

新增遥测、分析、远程配置或其他外联需要单独评审，并同步更新 [docs/privacy.md](docs/privacy.md)。不要声称存在尚未实现的授权开关、默认关闭或设置项。

## 提交与推送

本仓库的提交与推送由 agent 自动执行，不需要逐次确认。以下约束仍然适用：

- 只暂存本次改动涉及的文件，不夹带无关改动；提交前用 `git status` 确认范围。
- 提交信息使用中文，前缀遵循 Conventional Commits（`feat` / `fix` / `refactor` / `docs` / `test` / `chore`）。
- 推送前确认相关测试通过；涉及版本号变更时先完成 [docs/release.md](docs/release.md) 的核对项。
- 推送目标是 `origin/main`，不创建额外分支，不改写已推送的历史。

## 文档与发布

- 面向用户的文档使用中文，事实与代码一致；未知即未知，不臆测模型、账号或用量。
- 不宣称已在 Chrome 网上应用店上架，也不宣称未来 CI 已验证。
- 新增相对链接前确认目标文件存在；不要引入外部图片资源。
- 价格口径以 [docs/pricing.md](docs/pricing.md) 为准，成本是参考值，不是账单。
- 版本号以 `extension/manifest.json` 为唯一权威来源，其余位置按 [docs/release.md](docs/release.md) 手工同步。

<!-- 以下导入供本机 WorkBuddy 加载项目记忆；`.local/` 不入库，外部克隆者可忽略。 -->
@.local/memory.md
