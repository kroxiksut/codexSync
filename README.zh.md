<p align="center">
  <img src="assets/brand/brand-mark.png" width="96" alt="CodexSync">
</p>

<h1 align="center">codexSync</h1>

<p align="center">
  在你自己的几台机器之间安全地搬运 Codex 工作 —— 对话、项目列表、配置和本地状态 ——
  带有受保护的交接、冲突处理、经过校验的备份和恢复。
</p>

<p align="center">
  <a href="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml"><img src="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/kroxiksut/codexSync/releases"><img src="https://img.shields.io/github/v/release/kroxiksut/codexSync?include_prereleases&sort=semver" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-3776AB?logo=python&logoColor=white" alt="Python 3.11 | 3.12 | 3.13 | 3.14">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform: Windows | macOS | Linux">
  <img src="https://img.shields.io/badge/runtime%20dependencies-0-brightgreen" alt="Runtime dependencies: 0">
  <img src="https://img.shields.io/badge/GUI-PySide6%2C%20optional-41CD52?logo=qt&logoColor=white" alt="GUI: PySide6, optional">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue" alt="License: GPL-3.0-or-later"></a>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.ru.md">Русский</a> · <b>中文</b>
</p>

> [!IMPORTANT]
> 实测验证过的只有 Windows → Windows。Linux 和 macOS 处于实验阶段。在 Linux 上，进程检测已经对着真实运行的
> Codex 观察过，写入已开放，但还没有试过与 Linux 之间的交接（Codex 自己的 Linux 应用也还是预览版）。
> 在 macOS 上读取、诊断和计划都能用，但在 Mac 上做过同样的观察之前，写入一直关闭。

<p align="center">
  <a href="docs/zh/GUI.md"><img src="docs/screenshots/zh/02-overview.png" width="85%" alt="CodexSync 窗口：概览"></a>
</p>

Codex 把重要的工作状态保存在你的电脑上。把 `.codex` 放进 Dropbox、OneDrive 或
Syncthing 并不够：Codex 可能正在写入它，同一个对话可能在两台机器上被以不同方式继续，
而一次被中断的覆盖可能恰好毁掉你需要的那份副本。codexSync 把搬运这些状态当作一次
受保护的交接，而不是普通的文件同步。

## 它能做什么

- **在机器之间交接工作** —— 点一下，或在 Codex 关闭时自动进行：设置、对话和项目列表
  进入云文件夹，下一台机器载入它们，每台机器都知道另一台交接了什么。
  → [同步](docs/zh/SYNC.md#在电脑之间交接工作)
- **搬运对话和项目** —— 对话历史、对话名称和项目归属会出现在 Codex 查找它们的
  位置，即使项目文件夹在另一台机器上是另一条路径。项目自身的文件不会被复制：完整同步会
  比较文件夹并指出缺少什么。→ [项目与对话](docs/zh/PROJECTS.md)
- **绝不合并两段历史** —— 每个会话分支都会被分类；在两台机器上都被继续的对话只
  保留一份完整副本（按你的规则或选择），另一份被保存下来。→ [会话](docs/zh/SESSIONS.md)
- **每次覆盖前先备份**，并能从被中断的写入中恢复 —— 锁、事务日志、经过校验的备份。
  → [备份与恢复](docs/zh/RECOVERY.md)
- **检查并修复 Codex** —— Codex 无法启动或不显示聊天时，`codex check` 说明原因，`codex repair`
  修复能修复的问题，无论是谁造成的。→ [Codex 无法启动时](docs/zh/RECOVERY.md#codex-无法启动时)
- **守护全局状态**：*在 Codex 运行期间*拍摄经过校验的快照，并且只写到 `.codex`
  之外。→ [快照守护](docs/zh/GUARDIAN.md)
- **可以自动运行** —— 交接监视器、`.codex` 副本和定期快照都是操作系统的普通任务。
  → [自动化](docs/zh/CONFIGURATION.md#自动化)

它不是 Codex 客户端，也不做实时同步。它从不强制结束 Codex，对 Codex 的数据库只做三处
有限且先备份的修改：[它不做什么](docs/zh/README.md#它不做什么)。

## 隐私与安全

- **本地优先。** codexSync 没有自己的服务：共享状态只经过你选择的文件夹。
- **你的登录信息绝不外传。** `auth.json` 和其他凭据文件在任何层级都不会被复制。
- **只在 Codex 关闭时写入。** 无法确定时，什么都不写。如果你允许，同步会先请求 Codex 退出——绝不强制。
- **没有经过校验的备份，什么都不会被替换**；每次写入都记入事务日志，被中断的写入可以
  继续完成或回滚。

发现了安全或数据安全方面的问题？见 [SECURITY.md](./SECURITY.md)。

## 安装

**Windows，无需 Python：** 从 [Releases](https://github.com/kroxiksut/codexSync/releases)
下载 `codexsync-gui-…-windows-amd64.zip`（或 `…-arm64.zip`），解压后运行 `codexsync-gui.exe`。同一个可执行文件也能运行命令行子命令。需要 Windows 10（1809）或更新版本。

**使用 Python 3.11+**，在 Windows、macOS 或 Linux 上——0.2 处于 alpha 阶段时需要加 `--pre`，否则 pip 会安装 0.1；
加上 `--upgrade`，已安装的 0.1 也会被替换。在 macOS 上所有读取和计划都能用，但在进程检测器于那里得到验证之前，
写入会被拒绝；在 Linux 上它已经得到验证（[平台](docs/zh/README.md#平台)）。在 Linux 上系统自带的 Python 不允许 `pip install`（PEP 668），
请安装到虚拟环境中：
`python3 -m venv ~/.venvs/codexsync && ~/.venvs/codexsync/bin/pip install --pre "codexsync[gui]"`
（在 Ubuntu 和 Debian 上先执行 `sudo apt install python3-venv`）。

```powershell
python -m pip install --upgrade --pre "codexsync[gui]"    # 窗口和命令行
codexsync-gui                                             # 首次启动时，窗口会帮你创建或打开 config.toml

python -m pip install --upgrade --pre codexsync           # 只有命令行，没有依赖
codexsync init-config --output config.toml
codexsync -c config.toml doctor
```

从源码安装以及首次运行的详细步骤：[安装与上手](docs/zh/README.md#安装)。

## 文档

| | |
|---|---|
| [总览](docs/zh/README.md) | 工作方式、安装、首次运行、设计原则、平台 |
| [窗口](docs/zh/GUI.md) | 全部十三个界面及截图 |
| [命令行](docs/zh/CLI.md) | 每条命令、全局选项、退出码 |
| [配置](docs/zh/CONFIGURATION.md) | 逐节讲解 `config.toml`，以及自动化 |
| [同步](docs/zh/SYNC.md) | `plan` 与 `sync`：比较、冲突、方向、删除 |
| [快照守护](docs/zh/GUARDIAN.md) | 全局状态的快照、隔离区、还原、新的基准 |
| [会话](docs/zh/SESSIONS.md) | 跨机器的会话分支、工作集、镜像、索引 |
| [项目与对话](docs/zh/PROJECTS.md) | 对话归属、换机之后的修复、搬移项目 |
| [备份与恢复](docs/zh/RECOVERY.md) | 备份、还原、被中断的写操作、Codex 无法启动时 |

全部页面汇总在一处：[docs/](docs/README.zh.md)。面向贡献者（均为英文）：
[开发者文档](docs/dev/README.md)。更新日志：[CHANGELOG.md](./CHANGELOG.md)。

## 状态

**0.2.0 alpha 1** —— 第一个在命令行之外带上窗口（`codexsync[gui]` 或 `codexsync-gui.exe`）的版本。
之所以是 alpha，是因为 0.2 理解并修改的 Codex 状态比 0.1 多得多：它已在日常使用中，每次写入仍然经过锁、
日志和经过校验的备份，但在 0.2.0 之前，它需要在比我们更多的机器上运行。如果你在两台或更多机器上使用 Codex，
请试用并[告诉我们](https://github.com/kroxiksut/codexSync/issues/new/choose)结果。alpha 写入的数据，
之后的每个版本都能读取。

**从 0.1 升级。** codexSync 0.2 能识别 0.1 创建的配置，并就地升级它——在窗口里，或用
`config check` 和 `config upgrade`：0.1 的少数取值会被所有写入命令拒绝，升级会在修改前
逐项展示每个改动。现有状态保持不变，之前的配置保存在 `config-history/` 中。
→ [升级配置](docs/zh/CONFIGURATION.md#升级来自旧版本的配置)

有几项运行时行为被刻意暂不使用，直到受控实验把它们记录下来 —— 见
[还没有被验证的部分](docs/zh/README.md#还没有被验证的部分)。

## 许可

双重许可：开源部分采用 `GPL-3.0-or-later`（[LICENSE](./LICENSE)），商业路径见
[COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md)。贡献按
[CONTRIBUTING.md](./CONTRIBUTING.md) 和 [CLA.md](./CLA.md) 的条款接受。
