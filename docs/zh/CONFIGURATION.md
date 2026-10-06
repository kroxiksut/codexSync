# 配置

[English](../en/CONFIGURATION.md) · [Русский](../ru/CONFIGURATION.md) · **中文**

[← 文档](README.md)

codexSync 做的每一件事都由一个 `config.toml` 决定。可以用 `codexsync init-config`
创建它，也可以在窗口的「首次运行」页面创建，然后手工编辑或者在「设置」页面编辑。
每个键都带注释的完整模板是 [config.example.toml](../../config.example.toml)。

每条会写入的命令还会检查配置有没有要求安全规则所禁止的事情；这样的配置会以退出码 `4`
被拒绝。

## 路径与工作目录

```toml
[identity]
machine_id = "desktop"

[paths]
workspace_root_dir = "D:/Cloud/codexSync"
local_state_dir = "C:/Users/me/.codex"
cloud_root_dir = "${workspace_root}/sync"
backup_dir = "${workspace_root}/backups"
temp_dir = "${workspace_root}/.tmp"
```

- **`machine_id`** 唯一且长期不变。备份、快照、计划以及另一台机器上的映射规则都靠它
  来指认；两台机器同名会把各自的数据混在一起。
- **`workspace_root_dir`** 是由云客户端在机器之间同步的文件夹。其他任何路径里的
  `${workspace_root}` 都代表它。
- **`local_state_dir`** 是 Codex 自己的状态目录。它会被读取，只有在冷操作时才被写入，
  而且永远不会被创建。如果它不存在，codexSync 会改用 `CODEX_HOME` 或 `~/.codex`；
  每个命令都会先检查它自己的文件夹（下面这些，以及 Guardian、语义存储和副本的文件夹）
  都不在实际使用的那个目录之内——否则在读写任何东西之前以退出码 4 停止。
- **`cloud_root_dir`** 是状态在云文件夹中的镜像。
- **`backup_dir`** 存放在替换任何东西之前所做的备份。
- **`temp_dir`** 存放操作锁和变更日志，`restore` 也在这里解包快照。副本本身则在
  它目标所在的文件夹里暂存和校验：原子替换只在同一个卷内有效，而 `.codex` 常常和
  云文件夹不在同一个磁盘上。

相对路径按 `workspace_root_dir` 解析；没有工作目录时，则按 `config.toml` 所在的文件夹
解析。`cloud_root_dir`、`backup_dir`、`temp_dir` 和 `state.manifest_file` 不能互相
嵌套：它们各有一个按自己的节奏从中删除文件的所有者。带 `\\?\` 前缀的路径会去掉前缀
后再比较。

## 各节

| 节 | 控制什么 | 参见 |
|---|---|---|
| `[sync]` | 比较方式、方向、删除、默认试运行 | [同步](SYNC.md) |
| `[targets]` | `include_roots`：`.codex` 下哪些内容参与 `sync` | [同步](SYNC.md#同步哪些内容) |
| `[filters]` | `exclude_globs`：哪些内容永不复制 | [同步](SYNC.md#同步哪些内容) |
| `[conflict]` | 两侧都被改过的文件或聊天适用的 `policy`（默认：保留较新的） | [同步](SYNC.md#冲突) |
| `[backup]` | 备份的保留策略与格式 | [恢复](RECOVERY.md#备份) |
| `[guardian]` | 快照存储、轮询、减少阈值、保留策略 | [守护](GUARDIAN.md#设置) |
| `[semantic]` | 冲突包、镜像压缩 | [会话](SESSIONS.md#云端镜像) |
| `[[path_mappings]]` | 一台机器上的路径如何对应到另一台 | [见下](#path_mappings) |
| `[process_detection]` | 哪些进程意味着「Codex 在运行」 | [见下](#process_detection) |
| `[scheduler]` | 计划执行的安全作业 | [见下](#自动化) |
| `[state_backup]` | 登录后和/或每 N 小时制作 `.codex` 副本 | [见下](#codex-副本) |
| `[handoff]` | 在电脑之间交接工作，以及监视器任务 | [同步](SYNC.md#在电脑之间交接工作) |
| `[logging]` | 级别、格式、轮转、保留 | [见下](#日志) |
| `[safety]` | 固定不变：Codex 必须已停止，不确定就中止 | 不可编辑 |
| `[state]` | 同步清单放在哪里 | — |

## `[[path_mappings]]`

换机通常会改变路径：一个项目在台式机上位于 `D:/Projects/atlas`，在笔记本上位于
`C:/Work/atlas`。一条规则就是这么说的：

```toml
[[path_mappings]]
rule_id = "desktop-projects-to-laptop"
source_machine = "desktop"
target_machine = "laptop"
from = "D:/Projects"
to = "C:/Work"
# case_sensitive = false   # 可选
```

`rule_id` 必须唯一。这些规则由 `chats`、`repair-projects` 以及 `sessions` 的工作集使用；
它们永远不会被写进 Codex 的文件，Codex 也不会读取它们。见[项目与对话](PROJECTS.md)。

## `[process_detection]`

```toml
[process_detection]
process_names = ["codex.exe", "codex", "codex-app-server"]
grace_period_seconds = 2

[process_detection.background_process_names]
windows = ["codex-windows-sandbox", "codex-windows-sandbox-setup", "codex-command-runner"]
macos = ["ChatGPT.app/Contents/MacOS/", "codex-app-server", "codex-execve-wrapper", "codex-code-mode-host"]
linux = ["/usr/lib/chatgpt/", "codex-app-server", "codex-linux-sandbox", "codex-execve-wrapper", "codex-code-mode-host"]
```

只有在 Codex 关闭后会消失的进程名才应写入 `background_process_names`。始终运行的
进程在两种状态下给出相同的结果：它不会让检测更严格，而是让安全闸门永远报告
"正在运行"，从而永久阻止所有写入命令。因此 `codex-windows-sandbox-service`
不在列表中：在 Windows 上它是开机自动启动的服务
`CodexSandboxService.OpenAI.Codex`。

名称按完整进程名匹配，绝不按子串匹配。含有 `/` 的条目是路径标记，用来与进程路径匹配：
macOS 上的桌面版构建就叫 `ChatGPT`，而单写一个 `ChatGPT` 会把普通的 ChatGPT 应用也
匹配进来。

`allow_terminate_if_running` 以及其他 `terminate_*` 键是 0.1 遗留下来的。codexSync
从不结束 Codex，`allow_terminate_if_running = true` 会被拒绝。

## 升级来自旧版本的配置

0.1 随附的模板里写着 `allow_terminate_if_running = true` 和
`session_mode = "last_date_only"`，而本版本对任何会写入的命令都拒绝这两个值。
于是由 0.1 写出的 `config.toml` 会让 `sync`、`restore`、`repair-projects apply`
和 `recover` 以退出码 4 结束，直到它被升级为止 —— 而且光靠设置界面也改不了：
那里没有第一个键的字段，并且只要文本里还留着它，保存就会被拒绝。

```powershell
codexsync -c config.toml config check     # 本版本会改什么；不写入任何内容
codexsync -c config.toml config upgrade --confirm-plan <id>
```

`config check` 会打印每一条发现、它的代码、确切改动和差异，然后是计划 id。
`config upgrade` 需要那个 id，而 id 覆盖文件的字节，所以在这期间被编辑过的配置
会让升级停下来，而不是被覆盖。在窗口里，同样的内容出现在「设置」中的
**来自旧版本的配置**：同样的列表、同样的差异、同样的 id。

你的文件仍然是你的。注释会保留，数组按行增删而不是整体重写，计划没有点名的值
不会被碰，被替换的版本会复制到工作目录旁边的 `config-history/`。所有改动一次
写入：只修好第一个阻塞项的配置仍然会被拒绝，因此不存在「迁移了一半」的状态。

| 代码 | 级别 | 含义 |
|---|---|---|
| `TERMINATE_FLAG_SET` | 阻止写入 | `allow_terminate_if_running = true`；codexSync 从不结束 Codex |
| `SESSION_MODE_LAST_DATE` | 阻止写入 | `session_mode = "last_date_only"` 可能丢掉分支 |
| `BACKUP_DISABLED` | 阻止写入 | `backup_before_overwrite = false` |
| `DETECTION_LIST_OUTDATED` | 安全 | 你的列表里缺少本版本已知的 Codex 进程 |
| `MISSING_EXCLUDE_SKILLS_SYSTEM` | 正确性 | 没有排除 `skills/.system/**` |
| `MISSING_EXCLUDE_CODEX_BINARIES` | 正确性 | 没有排除 `plugins/` 下 Codex 自己的程序 |
| `OBSOLETE_INCLUDE_ROOT` | 正确性 | 那些本来也不会被复制的包含路径 |
| `SCHEDULER_INTERVAL_MIGRATED` | 正确性 | `interval_minutes` 已换算进 `interval_seconds` |
| `LEGACY_SCHEDULER_KEYS` | 正确性 | 本版本忽略的调度键 |

阻塞项必须解决；其余都可以拒绝 —— 命令行上用 `--skip CODE`，窗口里用旁边的勾选框。
`DETECTION_LIST_OUTDATED` 值得在拒绝之前读一读：进程名是整体比对的，所以配置里
没写的 Codex 进程根本不会被看见，而「窗口已关、后台进程还活着」正是那项安全检查
存在的理由。`doctor` 把这一切报告为 `config_compat`。

## 自动化

计划任务是 `[scheduler]` 的生效形式。请在这里设置，或者在窗口的「自动化」页面
设置，而不要直接改操作系统的计划任务。

```toml
[scheduler]
enabled = true
mode = "guardian_snapshot"   # guardian_snapshot | preflight | sync_dry_run
interval_seconds = 1800      # 300（5 分钟）到 2678400（31 天）；默认 1800（30 分钟）
run_at_login = true
startup_delay_seconds = 0
jitter_seconds = 0
sync_at_login = false        # 单独的任务：登录后同步一次设置
```

```powershell
codexsync -c config.toml automation status   # 配置、确切的命令、系统任务状态；不改变任何东西
codexsync -c config.toml automation apply    # 安装或更新任务；enabled = false 时删除它
codexsync -c config.toml automation remove   # 删除任务；config.toml 原样不动
codexsync -c config.toml automation run      # 立即执行一次配置好的作业
```

- 周期任务只能执行安全作业：`guardian_snapshot`、`preflight` 或 `sync_dry_run`。修复、
  传输、还原或回滚都无法排入计划，而且计划中的试运行在 Codex 开着时同样会被拒绝。
- **登录后同步**（`sync_at_login = true`，即「登录后同步设置」复选框）是唯一的例外，默认
  关闭。它会安装第二个任务，在您登录后等待 `startup_delay_seconds`，执行一次
  `sync --apply --unattended`，之后不再重复。它经过与手动同步相同的检查：Codex 打开时
  会被拒绝（退出码 3），冲突与任何一次运行一样由 `conflict.policy` 裁决；策略未能裁决的
  冲突会在写入前停止它（退出码 2）。它只同步设置目录 —— 任务从不传输会话。如果 Codex 随
  Windows 启动，任务运行时它已经打开，同步会直接跳过；若希望它生效，请把 Codex 从自启动
  中移除。窗口会显示该任务的上次运行及其结果含义。
- 它是用户级任务 —— Windows 上是任务计划程序，macOS 上是 LaunchAgent，Linux 上是
  `systemd --user` —— 绝不是系统服务。
- `automation run` 的退出码与该作业本身一致：成功、或者已有另一个守护在运行时为 `0`，
  进入隔离区为 `2`，`sync_dry_run` 期间 Codex 在运行为 `3`，失败为 `5`。

**已废弃：** `guardian scheduler` 和 `scripts/scheduler/{windows,macos}` 把计划任务的
设置放在 `config.toml` 之外，将来会被移除。如果你曾用那些脚本装过任务，请先用它们卸载，
以免两个任务同时运行。

**每个账户一个任务，以及搬了家的可执行文件。** Windows 任务注册在 `\CodexSync\`
文件夹下，名字是 `CodexSync Job (<用户>)`：在 0.2 之前所有账户共用一个名字，于是
一个用户启用自动化就会替换掉另一个用户的任务，关闭时又会把对方的删掉。属于其他
账户的任务现在只会被显示，永远不会被修改或删除。`automation status` 还会说明
已安装的任务实际运行什么，因此被改名或移动过的可执行文件 —— 升级打包版之后留下的
正是这种情况 —— 会报成 `EXECUTABLE_MISSING` 或 `EXECUTABLE_MOVED`，而不是含糊的
「与配置不符」；`automation apply` 会把它按这次安装重新注册。

## `.codex` 副本

Codex 状态目录中有价值部分的副本，只在 Codex 关闭时制作，并放进你选择的文件夹。在设置
这个文件夹之前它一直是关闭的：不会自动推荐任何位置，因为由云客户端同步的文件夹会上传
每一个副本。

```toml
[state_backup]
root_dir = "E:/Backups/codex"   # 为空：不制作副本，任务保持关闭
at_login = true                 # 登录后制作一个副本
interval_hours = 24             # 和/或每 N 小时一次，最多 744；0 = 关闭
keep = 5                        # 保留本机的副本数量
```

```powershell
codexsync -c config.toml state-backup create          # 立即制作一个副本；Codex 打开时退出码为 3
codexsync -c config.toml state-backup create --wait   # 先等待 Codex 关闭（最多 23 小时）
codexsync -c config.toml state-backup list            # 文件夹中的副本
codexsync -c config.toml automation apply             # 安装负责制作副本的任务
```

- **复制哪些内容：**会话和归档、全局状态及其 `.bak`、SQLite 目录（`state_*`、
  `thread_history_*`、`memories_*`、`goals_*`，连同它们的 `-wal`/`-shm`）、
  `session_index.jsonl`、`config.toml`、`AGENTS.md`、`rules`、`skills`、`memories`
  和 `automations`。**从不复制：**`auth.json`、`cap_sid`、`.sandbox-secrets` 以及其他
  保存令牌的内容，也不复制缓存、日志、沙盒、临时文件和任何已编译的程序（安装程序会恢复它们）。在开发所用的机器上，压缩前约
  1.2 GB。Codex 自己的 `config.toml` 会被完整复制，其中可能含有 MCP 服务器的令牌
  （`env` 表、bearer 标头）：请把 `root_dir` 放在只有你能读取的位置，不要放在共享文件夹中。
- **不跟随链接。** `.codex` 中的符号链接或 Windows 目录联接（`skills` 里常有指向其他
  文件夹的联接）连同其后的一切都会被跳过；云盘占位文件是普通文件，照常复制。无法列出的
  文件夹会使副本失败（退出码 5），而不是在副本里留下缺口。
- **只在 Codex 关闭时。**正在写入的数据库或会话的副本不对应任何确定的时刻，因此复制与
  写入经过同样的进程检查：在第一个文件之前 Codex 必须持续处于关闭状态，在副本计入之前
  还会再检查一次。计划任务会等待 Codex 关闭（每 30 秒检查一次，最多 23 小时）；不带
  `--wait` 的 `create` 和窗口里的「立即制作副本」则会拒绝（退出码 3）。读取期间发生
  变化的文件会使副本失败。
- **每个副本一个 zip**，`codex-<机器>-<UTC 时间>.zip`，其中包含每个文件及其 SHA-256
  的清单。它先写成 `.partial`，读回并比对之后才会改名。之后会删除本机超出 `keep` 的
  最旧副本；其他机器留在同一文件夹中的副本只会列出，从不删除。
- `root_dir` 必须位于 `.codex`、`backup_dir`、`temp_dir`、云镜像、`guardian.root_dir`
  和 `semantic.root_dir` 之外：这些目录各有自己的清理、打扫或镜像规则。
- 这是第三个用户级任务 `CodexSync Codex backup (<用户>)`；登录后它同样会先等待
  `startup_delay_seconds`。副本只读取 `.codex`，放回去需要手动完成：关闭 Codex，解压
  你需要的内容。
- 安装任务本身不会制作副本：开启 `at_login` 时，第一个副本在下次登录时制作，各平台
  一致。该任务依赖进程检测，而它只在 Windows 上得到验证，因此在 macOS 和 Linux 上
  `automation apply` 会拒绝安装它（以及 `sync_at_login`），而不是安装一个每次运行都以
  退出码 5 结束的任务。

## 日志

```toml
[logging]
level = "INFO"            # DEBUG | INFO | WARNING | ERROR
file = "${workspace_root}/logs/codexsync.log"
format = "text"           # text | json | logfmt
retention_days = 7
archive_mode = "zip"      # zip | text
max_file_size_mb = 10
```

- 日志文件按天分开，并带上机器标识：`<名称>-<机器>-YYYY-MM-DD[.N].log`，UTF-8 编码。
- 文件按天和按大小轮转；旧文件会归档成 `.zip`（`archive_mode = "zip"`）或保留为文本，
  并在 `retention_days` 之后删除。
- 每一个危险动作都会单独记入日志：创建备份、覆盖、跳过。
- `file` 必须位于 `.codex` 之外，`level` 必须是列出的取值之一。
- `plan`、`sync`、`restore` 以及计划任务运行的命令（`guardian`、`preflight`、
  `state-backup`、`handoff`）会写入这个文件。
