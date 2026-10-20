# 第三方组件与许可

Agent Usage Desk 本体以 MIT 许可发布（见仓库根目录 `LICENSE`）。本文说明源码仓库与 Windows 发布包中包含的其他组件，以及分发时需要注意的事项。

**这不是一份完整的第三方许可审计。** 当前仓库没有证据表明已经逐项核对所有随附二进制、编译工具与素材的许可条款；在对外分发二进制之前，分发者仍需自行确认并保留相应许可文本。

## 源码仓库

| 组件 | 用途 | 许可/归属 | 是否随包分发 |
|---|---|---|---|
| Python 标准库 | 本地读取器运行时 | Python Software Foundation License | 二进制包中随附 |
| Node.js `node:test` 等内置模块 | 开发测试 | Node.js 项目许可 | 否 |
| Playwright + Chromium | 可选的界面测试 | Apache-2.0 / Chromium 各自许可 | 否 |

仓库没有 `package.json`、npm 依赖或前端打包器；扩展的 JavaScript 直接由 Chrome 加载。测试只使用 Node 内置模块。

## Windows 发布包

`scripts/build.py` 从本机 Python 3.12 安装目录复制运行时，因此发布包会包含：

- `python.exe`、`python3xx.dll`、`python3.dll`、`DLLs\*.pyd`、`DLLs\*.dll` 与 `stdlib.zip`；
- `vcruntime*.dll`（Microsoft Visual C++ 运行时，通常随 Python 一起分发）；
- `runtime\LICENSE.txt`（构建脚本直接复制，用于保留 Python 的许可文本）。

分发这些文件时，至少需要：

1. 保留 Python 的许可文本（`runtime\LICENSE.txt`），并说明使用的是 CPython；
2. 保留或附带 Microsoft Visual C++ 可再发行组件的适用条款；
3. 不删除本项目的 `LICENSE` 与 `THIRD-PARTY.md`。

`CodexMonitorHost.exe` 由 Windows 自带的 .NET Framework 4 编译器 `csc.exe` 从 `launcher/Launcher.cs` 编译，仓库不包含第三方 NuGet 包或额外的 .NET 运行库。编译器本身只用于构建，不随发布包分发。

## 从源码构建与分发二进制的区别

- **从源码使用**：使用者自行运行 `scripts/build.py`，运行时与 DLL 来自其本机 Python 安装；许可义务主要由使用者本地环境承担。
- **分发二进制**：发布者把 Python 运行时、`vcruntime` 与 DLL 一并打包，等于在分发第三方二进制，需要自行确认再分发权利、保留许可文本，并遵守各组件的通知要求。MIT 只覆盖本项目自己的代码，不改变这些第三方条款。

仓库中的扩展图标等图片素材由项目自带，未引用外部图片资源；其来源与授权未在本次公开整理中另行审计。

## 商标与非隶属声明

Codex、OpenAI、WorkBuddy、CodeBuddy、腾讯、DeepSeek 等名称与商标归各自权利人所有。本项目是独立的非官方工具：

- 与上述公司及产品没有隶属、合作、赞助或背书关系；
- 名称仅用于说明所读取的本地日志格式与兼容对象；
- 不附带任何官方支持或服务承诺。

如果权利人认为某项使用不妥，请通过仓库 Issues 联系维护者。
