# 隐私说明

本文描述 Agent Usage Desk 0.3.3 的实际行为。目标是让统计在本机完成，同时如实说明哪些操作会访问网络。

本项目为非官方个人项目，与 OpenAI、腾讯、DeepSeek 等公司及其产品无隶属、合作或背书关系。项目名称与界面品牌只用于说明兼容的本地数据来源。

## 1. 读取哪些本机数据

扩展本身不读文件，读取由 `%LOCALAPPDATA%\CodexMonitor\app` 下的 Python 本地读取器完成。

| 来源 | 默认目录 | 读取方式 |
|---|---|---|
| Codex | `%USERPROFILE%\.codex`（可用 `CODEX_HOME` 覆盖） | 只读扫描 `sessions`、`archived_sessions` 中的 `.jsonl` 日志 |
| WorkBuddy 桌面端 | `%USERPROFILE%\.workbuddy\projects` | 只读扫描项目日志 |
| CodeBuddy CLI | `%USERPROFILE%\.codebuddy\projects` | 只读扫描项目日志 |
| CLI 续接证据（可选） | `%LOCALAPPDATA%\CodexDeepSeek\runs` | 只读扫描运行记录 |

以上目录都可以在扩展设置中修改；留空则使用默认位置。缺少某个目录时，该来源显示为不可用，不影响其他来源。

原始日志始终以只读方式打开；本项目不会写入、移动或删除源日志。

## 2. 本机索引与缓存

数据保存在 `%LOCALAPPDATA%\CodexMonitor`：

- `index-<哈希>.sqlite3`（含 SQLite WAL 附属文件）：Codex 请求与每日汇总索引。
- `buddy-index.sqlite3`（含 SQLite WAL 附属文件）：WorkBuddy 桌面端与 CodeBuddy CLI 的请求索引。
- `buddy-quota.json`：WorkBuddy 额度的最近一次成功结果。
- `config.json`：用户选择的日志目录与 Codex 目录。
- `import.lock`：防止并发导入的锁文件。

索引保存的是统计数据与定位信息，例如：请求时间、模型名、输入/输出/缓存 token 数、耗时、速度、参考成本、模式（standard/fast）、会话编号、来源文件路径，以及额度的百分比、积分数量、套餐名和积分包名称/到期时间。

索引**不保存聊天正文、提示词、回复内容或工具输出**。解析器只提取用量与计时字段；单元测试也会检查索引文件中不出现日志正文。会话编号是标识符，不是对话内容。设置界面中的目录路径会以明文保存在 `config.json`。

额度快照可能包含套餐名称、剩余/已用积分、冻结积分与积分包到期时间，但不包含访问令牌。

## 3. 网络访问

本项目没有自建服务器、遥测、分析 SDK 或云同步。网络访问只发生在账号额度查询，分为两条独立路径。

### 3.1 Codex 订阅额度

读取器会以当前用户的 `CODEX_HOME` 启动本机 `codex app-server` 子进程，通过标准输入输出调用 `account/rateLimits/read`，只保留额度窗口的已用百分比、窗口时长与重置时间。网络请求由 Codex 自身按其登录状态完成，本项目不直接连接 OpenAI 接口，也不读取 Codex 的凭据文件。

若账号接口不可用，会尝试从最近的 Codex 日志尾部读取历史额度字段作为回退；仍不可用则显示未知。

### 3.2 WorkBuddy 个人账号额度

查询时只读打开 WorkBuddy 桌面端的登录凭据文件：

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

处理规则：

- 文件只读打开，大小上限 64 KiB；仅接受 `account.type` 为 `personal`、无企业标识、`auth.domain` 为 `www.workbuddy.cn` 且未过期的凭据。
- 访问令牌只放入请求头，用于以下三个固定地址，不缓存、不写日志、不返回给扩展界面：

  ```text
  https://www.workbuddy.cn/billing/meter/get-user-resource-summary
  https://www.workbuddy.cn/billing/meter/get-user-resource-paid-packages
  https://www.workbuddy.cn/billing/meter/get-user-resource-free-packages
  ```

- 不跟随重定向；响应大小与分页数量都有上限。
- **没有令牌刷新、登录、迁移或回退逻辑**：登录生命周期完全由 WorkBuddy 负责。凭据缺失、退出、过期或账号类型不支持时，只显示对应的中文提示。
- 额度缓存以账号标识的哈希作为键；切换账号会清除旧缓存。缓存内容不含令牌。

### 3.3 查询时机

- 扩展每 5 分钟触发一次额度更新，用户也可以点击“更新额度”立即查询。
- Codex 与 WorkBuddy 相互独立：一方失败不会隐藏另一方的结果。
- 当前代码**没有单独的额度授权开关**，设置中也没有关闭额度查询的选项。安装并运行本项目后，只要本机存在已登录账号，就会按上述行为发起查询。本次公开整理只调整文档与品牌，不新增或改变业务行为。

## 4. 处理失败与未知

- 网络失败、接口异常、字段缺失或无法验证时，界面显示“未知/读取失败”，不会把缺失值记为 0。
- 短暂网络失败且存在上次成功结果时，会标注“可能过期/上次成功”，并保留上次数值。
- 授权失效（HTTP 401/403）会清除旧额度，避免展示失效账号的数据。
- 企业版或其他不受支持的 WorkBuddy 账号会明确提示，不会尝试绕过限制。

## 5. 可选桥接目录

`%LOCALAPPDATA%\CodexDeepSeek\runs` 是本地开发工具产生的运行记录，用于把 CLI 会话与续接结果对应起来。它**不属于腾讯 CLI 的标准功能**，也不是本项目运行的必需条件：

- 不安装或没有该目录时，Codex、WorkBuddy 与 CodeBuddy CLI 的请求统计仍可正常使用；
- 没有对应证据时，续接状态显示为未知，不做推断；
- 该目录只读，不上传。

## 6. 数据保留与删除

- 卸载脚本删除注册项与 `app` 目录，但按设计保留 `%LOCALAPPDATA%\CodexMonitor` 中的索引与额度缓存。
- 如需彻底清除，请在卸载后手动删除 `%LOCALAPPDATA%\CodexMonitor`。
- 删除索引不会影响源日志；下次刷新会重新建立索引。

## 7. 扩展权限

`manifest.json` 只声明 `alarms`（定时刷新）、`storage`（保存显示偏好）与 `nativeMessaging`（与本机读取器通信）。扩展不申请网页访问权限，也不注入内容脚本。
