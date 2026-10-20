# 发布与版本核对

本文只覆盖本仓库的发版流程：版本号需要同步的位置，以及打包分发前的核对项。环境要求与构建细节见 [development.md](development.md)，对外行为承诺见 [privacy.md](privacy.md)。

## 版本号的权威来源

`extension/manifest.json` 的 `version` 是唯一权威来源。`scripts/build.py` 读取它生成安装包名 `dist\AgentUsageDesk-<版本>-windows.zip`，因此改版本时先改这里，产物名会自动跟随。

其余位置不会被自动同步，需要手工修改。

## 需要同步的位置

| 文件 | 需要更新的内容 | 匹配数 |
|---|---|---|
| `extension/manifest.json` | `version` | 1 |
| `native/codex_monitor/__init__.py` | `__version__` | 1 |
| `native/codex_monitor/buddy_quota.py` | `USER_AGENT` 中的 `CodexMonitor/<版本>` | 1 |
| `README.md` | 首段「当前版本 x.y.z」与构建产物的 zip 文件名 | 2 |
| `AGENTS.md` | 首段版本号 | 1 |
| `docs/privacy.md` | 首段「本文描述 Agent Usage Desk x.y.z 的实际行为」 | 1 |
| `tests/test_buddy_quota.py` | User-Agent 断言 | 1 |
| `tests/ui_v032.py` | 合成状态中的 `appVersion` | 1 |

合计 9 处。修改完成后自查，输出行数应为 9：

```powershell
Select-String -Path AGENTS.md,README.md,docs\privacy.md,extension\manifest.json,native\codex_monitor\__init__.py,native\codex_monitor\buddy_quota.py,tests\test_buddy_quota.py,tests\ui_v032.py -Pattern '<版本号>'
```

以下位置也含版本号字样，但不参与运行或构建，不需要同步：

- `docs/development.md` 末尾带日期的验证记录段落，属于历史快照，保留当时验证的版本；
- `extension/dashboard.css` 中标注版本的设计注释；
- `tests/ui_v032.py` 的文件名（脚本名固定，内部内容仍随版本更新）。

## 发布前核对

逐项确认后再打包分发。

### 版本与文档

- [ ] 上表八行对应的 9 处版本号一致，自查命令输出 9 行
- [ ] `README.md` 的功能列表与当前实际行为一致，没有描述尚未实现的能力
- [ ] `docs/privacy.md` 与真实网络行为一致：仅访问三个固定计费地址、不刷新令牌、索引不保存聊天正文
- [ ] 新增的相对链接都指向存在的文件

### 测试

```powershell
$env:PYTHONPATH = (Join-Path $PWD 'native')
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/background.test.mjs tests/buddy.test.mjs tests/quota-ring.test.mjs tests/usage-chart.test.mjs
```

- [ ] Python 单元测试全部通过
- [ ] Node 测试全部通过
- [ ] 界面测试（可选）截图与上一版对比，没有意外变化

### 安装包

```powershell
python .\scripts\build.py
```

- [ ] 构建成功，`dist\AgentUsageDesk\` 与 `dist\AgentUsageDesk-<版本>-windows.zip` 均存在
- [ ] 在未安装过本项目的环境中实际执行 `install.ps1`，扩展可加载且看板能出数
- [ ] 覆盖升级后按提示在 `chrome://extensions` 重新加载扩展，扩展 ID 未变
- [ ] `uninstall.ps1` 能清除注册项与 `app` 目录

### 数据边界

- [ ] 新增文件、截图与提交信息不含真实用户名路径、会话编号或额度数据

## 发布时不视为已完成的事项

以下事项与发版无关，但也不应在文档中写成已经完成：

- 第三方二进制的完整许可核对，见 [third-party.md](third-party.md)；
- 安装包尚未在 Chrome 网上应用店分发，当前仅支持「加载已解压的扩展程序」安装。
