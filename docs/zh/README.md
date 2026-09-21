# codexSync 文档

[English](../en/README.md) · [Русский](../ru/README.md) · **中文**

[← 项目主页](../../README.zh.md)

| 页面 | 内容 |
|---|---|
| [窗口](GUI.md) | 全部十一个界面及截图 |
| [命令行](CLI.md) | 每条命令、全局选项、退出码 |
| [配置](CONFIGURATION.md) | 逐节讲解 `config.toml`，以及自动化 |
| [同步](SYNC.md) | `plan` 与 `sync`：比较、冲突、方向、删除 |
| [快照守护](GUARDIAN.md) | 全局状态的快照、隔离区、还原、新的基准 |
| [会话](SESSIONS.md) | 跨机器的会话分支、工作集、镜像、索引 |
| [项目与对话](PROJECTS.md) | 对话归属、换机之后的修复、搬移项目 |
| [备份与恢复](RECOVERY.md) | 备份、还原、被中断的写操作 |

[![CodexSync 窗口](../screenshots/zh/01-overview.png)](GUI.md)

*覆盖在与命令行相同内核之上的窗口 —— [每个界面及截图](GUI.md)。*

## 为什么需要它

开发者想换一台机器继续用 Codex 工作，又不想丢掉它的本地状态：项目、每个对话属于
哪个项目，以及每个会话的历史。Codex 把这些都放在本地目录（`.codex`）里，而单靠一个
云文件夹无法安全地搬运它们 —— 在 Codex 正在写入时被复制的文件，或者在两台机器上都
增长过的历史，都会毫无报错地丢失。

## 它是怎么工作的

1. **Codex 在运行吗？** *无法判定* 的答案一律算作正在运行：任何改变状态的动作都不靠
   乐观假设。
2. **读取永远被允许。** `doctor`、`plan`、各种 `scan`、`chats` 和守护，无论 Codex
   是否开着都能运行。在 Codex 开着时得到的结果会被标记为 `volatile`，任何写入都不会
   重用它。
3. **写入只在 Codex 关闭时进行，而且永远是同一套流程：** 谁也抢不走的锁、可靠的事务
   日志、对将被替换的一切所做的、经过校验的备份、提交前紧接着的最后一次进程检查、
   同一磁盘卷上的暂存、一次原子替换，然后写下 `COMMITTED`。
4. **任何含糊之处都会停下来**，而不是去猜，并给出说明这是哪一类停止的
   [退出码](CLI.md#退出码)。

## 安装

内核和命令行没有任何依赖。窗口是可选的附加部分。

```powershell
git clone https://github.com/kroxiksut/codexSync
cd codexSync
pip install .            # 只装命令行
pip install ".[gui]"     # 命令行加窗口（PySide6）
```

在 Windows 上也可以直接从
[Releases](https://github.com/kroxiksut/codexSync/releases) 下载构建好的版本，
它们都不需要安装 Python：`codexsync-gui-<tag>-windows-amd64.zip`（窗口，同时也是
命令行）或 `codexsync-<tag>-windows-amd64.zip`（只有命令行，不含 Qt）。

## 首次运行

**窗口：**

```powershell
codexsync-gui
```

「首次运行」页面会创建 `config.toml`：机器名称、本地 `.codex`，以及同步的工作
目录。全部界面见[窗口](GUI.md)。

**命令行：**

```powershell
codexsync init-config --output config.toml --machine-id desktop --local-state-dir C:/Users/me/.codex --workspace-root D:/Cloud/codexSync
codexsync -c config.toml doctor           # 只读诊断
codexsync -c config.toml sync --dry-run   # 一次同步会做什么，不写入任何内容
codexsync -c config.toml sync --apply     # Codex 必须已关闭
```

每条命令及其选项见[命令行](CLI.md)；`init-config` 写出的文件在[配置](CONFIGURATION.md)
里逐节解释。

## 设计原则

- **先备份，出错就关闭。** 不确定性永远不会被乐观地解释掉。
- **一个权威，一套流程。** 有且只有一个地方决定状态是否可以改变，有且只有一条路径
  执行这个改变。
- **说明原因，不要猜。** 无法归类的分支、同时匹配两个候选项的项目、谁都没有观察过的
  运行时行为 —— 每一种都以代码报告出来，而不是近似处理。
- **先出计划，再按标识确认。** 每条会写入的命令都先给出计划或试运行，并且只执行你
  原样报回的那个计划标识。其间只要有任何变动，标识就不再匹配，也就什么都不会写入。
- **不与 Codex 的内部实现打交道。** codexSync 从不启动或结束 Codex，不读取令牌，
  也不写入 SQLite。
- 内核与命令行**零运行时依赖**。

## 交接流程

codexSync 假定机器之间有严格的先后顺序：

1. 在机器 A 上关闭 Codex。
2. 等待云客户端把机器 A 的改动完整上传完毕。
3. 在机器 B 上运行 codexSync。
4. 同步结束之后，才在机器 B 上启动 Codex。
5. 在机器 B 上重新登录 Codex。

认证令牌永远不会被搬运。codexSync 有意不检查云服务商的同步状态、云客户端进程，
以及云文件夹的剩余空间：这些由用户负责。

## 它不做什么

- 不与 Codex 的内部实现集成，不使用其 API，不拦截网络。
- 不提取令牌：交接之后请重新登录 Codex。
- 从不启动或结束 Codex，也从不写入 Codex 的 SQLite 数据库。
- 没有实时同步：同一时间只有一台机器在工作。
- 不检查云客户端，也不检查云文件夹的剩余空间。

## 平台

- **Windows** 是经过实测的平台。
- **macOS**（Apple Silicon）在代码和 CI 中受支持。macOS 与 Linux 的进程检测器已经
  写好，并针对记录下来的 `ps` 输出做过测试，但在还没有对着真实运行的 Codex 跑过之前，
  该平台会把自己报告为不受支持：进程状态读作「无法判定」，因而每条会写入的命令都会
  拒绝执行。
- **Linux** 的运行时支持目前不在范围内。
- CI 在 `windows-latest` 和 `macos-latest` 上用 Python 3.11、3.12 和 3.13 跑测试。

## 还没有被验证的部分

Codex 运行时的某些行为无法从它的文件中推知，只能观察。在一次针对可丢弃状态的受控
实验把它记录下来之前，codexSync 会报告这种情况，而不是去猜：

| 什么 | 如何表现 | 实验 |
|---|---|---|
| 把传输过来的会话分支写*入* `.codex` | `BLOCKED_UNPROVEN_LAYOUT` | [session-layout-adapter](../dev/experiments/session-layout-adapter.md) |
| 改写 `session_index.jsonl` | `UNPROVEN_CONSUMER_CONTRACT` | [session-index-contract](../dev/experiments/session-index-contract.md) |
| 在 macOS 和 Linux 上检测 Codex | 平台不受支持，写入被拒绝 | [process-detector-macos](../dev/experiments/process-detector-macos.md) |
| 存放在 `state_*.sqlite` 里的项目 | 搬移项目只改写 JSON；删除或合并项目根本不提供 | [project-registry-contract](../dev/experiments/project-registry-contract.md) |

最后一条，`doctor` 每次运行都会报告。

（开发者文档 `docs/dev/` 只有英文版。）
