# 命令行

[English](../en/CLI.md) · [Русский](../ru/CLI.md) · **中文**

[← 文档](README.md)

```powershell
codexsync -c config.toml <命令>                # 已安装
python -m codexsync -c config.toml <命令>      # 直接用源码检出，不安装
codexsync-gui.exe -c config.toml <命令>        # Windows 窗口版构建，不弹控制台
```

全局选项：`-c/--config` —— `config.toml` 的路径；`-v/--verbose` —— 详细日志，包括
看到了哪些 Codex 进程；`-V/--version` —— 打印版本并退出，用它可以询问下载到的
exe 究竟是什么版本。在任何命令或子命令后加 `-h` 会打印它的选项。

## 全部命令

「冷」表示这条命令在 Codex 关闭之前拒绝运行。其余命令只读取（或只写到 `.codex`
之外），任何时候都可以运行。

| 命令 | 冷？ | 作用 | 详情 |
|---|---|---|---|
| `init-config` | — | 用内置模板写出一个 `config.toml` | [见下](#上手) |
| `validate` | 否 | 加载并检查配置，仅此而已 | [见下](#上手) |
| `config check` | 否 | 报告本版本会对 `config.toml` 做哪些改动 | [配置](CONFIGURATION.md#升级来自旧版本的配置) |
| `config upgrade` | 否 | 以一次确认过的写入应用这些改动 | [配置](CONFIGURATION.md#升级来自旧版本的配置) |
| `doctor` / `preflight` | 否 | 环境诊断；两者相同且无副作用 | [见下](#上手) |
| `plan` | 否 | 显示一次同步会复制什么（Codex 开着时标记为 `volatile`） | [同步](SYNC.md) |
| `sync` | **是** | 双向复制状态，先备份 | [同步](SYNC.md) |
| `restore` | **是** | 从经过校验的备份还原文件 | [恢复](RECOVERY.md#还原一份备份) |
| `guardian watch` | 否 | 在 Codex 运行期间持续拍摄全局状态的快照 | [守护](GUARDIAN.md) |
| `guardian snapshot --once` | 否 | 立即拍一张快照 | [守护](GUARDIAN.md) |
| `guardian list` | 否 | 列出本机的快照和隔离区 | [守护](GUARDIAN.md) |
| `guardian restore` | **是** | 把一张经过校验的快照写回（不加 `--confirm` 即为预览） | [守护](GUARDIAN.md#还原一张快照) |
| `guardian accept` | 否 | 在确实发生减少之后，把当前状态作为新基准 | [守护](GUARDIAN.md#接受新的基准) |
| `guardian scheduler` | 否 | 已废弃：请用 `automation apply` | [配置](CONFIGURATION.md#自动化) |
| `automation status` / `run` | 否 | 显示计划任务，或立即执行它的安全作业 | [配置](CONFIGURATION.md#自动化) |
| `automation apply` / `remove` | 否 | 让系统任务与 `[scheduler]` 一致，或删除它 | [配置](CONFIGURATION.md#自动化) |
| `sessions scan` | 否 | 对两侧的每个会话分支分类 | [会话](SESSIONS.md) |
| `sessions resolve` | 否 | 为一处分叉记录一个决定 | [会话](SESSIONS.md#分叉) |
| `sessions apply` | **是** | 按一份已确认的计划整体传输分支 | [会话](SESSIONS.md#执行计划) |
| `sessions index` | 否 | 报告两边的 `session_index.jsonl` 各有什么 | [会话](SESSIONS.md#会话索引) |
| `chats list` / `chats tree` | 否 | 查找对话，看每个对话属于哪个项目 | [项目](PROJECTS.md#对话) |
| `chats move` | **是** | 把选定的对话放到一个项目下 | [项目](PROJECTS.md#把对话移到某个项目) |
| `repair-projects scan` | 否 | 构建一份不可变的、带哈希的修复计划 | [项目](PROJECTS.md#换机之后的修复) |
| `repair-projects apply` | **是** | 按标识执行某一份确切的计划 | [项目](PROJECTS.md#换机之后的修复) |
| `project-move scan` | 否 | 为项目计算哈希，并规划复制到新文件夹 | [项目](PROJECTS.md#搬移项目的文件) |
| `project-move apply` | **是** | 复制、校验，然后让 Codex 指向新文件夹 | [项目](PROJECTS.md#搬移项目的文件) |
| `recover inspect` | 否 | 无副作用地读取一条写操作日志 | [恢复](RECOVERY.md#被中断的写操作) |
| `recover resume` / `rollback` | **是** | 收尾一个被中断的写操作 | [恢复](RECOVERY.md#被中断的写操作) |

对每条会写入的命令都成立、且不可配置的两条规则：Codex 开着*或状态无法判定*时它拒绝
执行；替换任何东西之前它都会做一份经过校验的备份。

## 上手

把模板写到当前文件夹的 `config.toml`、写到别的路径，或覆盖已有文件：

```powershell
codexsync init-config
codexsync init-config --output D:\codexSync\config.toml
codexsync init-config --output D:\codexSync\config.toml --force
```

也可以直接写出一份已经为本机填好的配置。它会被校验，模板中的每条注释都会保留，
`--machine-id`、`--local-state-dir` 和 `--workspace-root` 必须一起给出
（`--cloud-root` 可选），而且这种模式下永远不会覆盖已有文件：

```powershell
codexsync init-config --output config.toml --machine-id laptop-1 --local-state-dir C:/Users/me/.codex --workspace-root D:/Cloud/codexSync
```

### 打开的是哪个配置

`-c` 指明文件，而指向一个还不存在的路径就是要求在那里创建它。不带 `-c` 时，
搜索顺序与窗口一致：当前目录下的 `config.toml`，然后是可执行文件旁边，最后是
按用户的位置 —— Windows 上是 `%APPDATA%\CodexSync\config.toml`，macOS 上是
`~/Library/Application Support/CodexSync/`，Linux 上是
`$XDG_CONFIG_HOME/codexsync/`。在当前目录以外找到的文件会被明确说明，这样命令
就不会悄悄地对着一个你没在看的文件工作。

窗口会记住上次打开的配置并以它启动；命令行什么都不记，所以在脚本或计划任务里，
`-c` 仍然是把话说准的方式。

先检查配置，再检查环境：

```powershell
codexsync -c config.toml validate
codexsync -c config.toml doctor
```

`doctor`（以及与它相同的 `preflight`）只读取、不创建任何东西 —— 尤其不会在 `.codex`
里面创建。它会检查配置和各个目录、Codex 是否在运行、全局状态的结构与最近一张可还原的
快照、会话文件与会话索引、SQLite 线程目录、一次同步被允许做什么、同步清单，以及遗留的
临时文件。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 运行时错误 |
| `2` | 发现冲突，需要做决定 |
| `3` | Codex 正在运行（冷前提不满足） |
| `4` | 配置或参数无效 |
| `5` | 安全中止（fail-safe） |

`doctor`/`preflight` 在全部检查通过、或只有警告时返回 `0`，至少有一项失败时返回 `5`。

## 进程安全

- codexSync 从不启动或结束 Codex。旧的「结束进程」开关会以退出码 `4` 被拒绝，对会写入
  的命令而言 `allow_terminate_if_running = true` 同样如此。
- 一次写入要求 Codex 已经连续停止两秒，另加提交前和提交过程中的直接检查。
- `RUNNING` 和 `UNKNOWN` 都会阻止写入。在 macOS 和 Linux 上，写入一直被阻止，直到
  进程检测器在真实机器上得到验证（见
  [还没有被验证的部分](README.md#还没有被验证的部分)）。
- 如果目标文件一瞬间被别的进程占用（云客户端、搜索索引器、杀毒软件），原子替换会以
  有上限的退避重试。每次重试之前都会重新做进程检查，而其他任何错误都不会重试
  （[D-011](../dev/DECISIONS.md)，英文）。
- 哪些后台进程算作「Codex 仍在运行」，按操作系统列在
  [`process_detection.background_process_names`](CONFIGURATION.md#process_detection) 中。
- 加上 `-v` 之后，`plan`、`sync` 和 `restore` 会记录被追踪的进程：Codex 及其沙箱是否在
  运行，以及 Codex 之下的子进程（PID、名称、父 PID）。完整命令行永远不会被收集。
