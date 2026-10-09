# 备份与恢复

[English](../en/RECOVERY.md) · [Русский](../ru/RECOVERY.md) · **中文**

[← 文档](README.md)

每条会写入的命令都会为它将要替换的内容做一份经过校验的备份，并在工作期间保留一份事务
日志。本页讲的就是这两件事，以及从中途停下的写入中脱身的办法。在窗口里，这对应
[备份](GUI.md#备份)和[恢复](GUI.md#恢复)两个页面。

## 备份

```toml
[backup]
backup_before_overwrite = true   # 设为 false 时写入会被拒绝
retention_days = 30
max_backups = 0                  # 0 = 不限
compression = "none"             # none | zip
```

- 备份放在 `paths.backup_dir` 下，形式是目录树（`none`）或单个 `.zip` 文件（`zip`）。
- 每份新备份都带着一份经过校验的 `codexsync-backup-v1` 清单。自动还原只会挑选带清单
  且已提交的备份。
- 备份按 `retention_days` 和 `max_backups` 清理——只清理这台机器自己的快照：不清理
  另一台机器的快照或只是放在 `backup_dir` 里的文件夹，也永远不清理这台机器上某个未完成的
  操作在 [`recover`](#被中断的写操作) 时仍需要的快照。唯一的例外是**冲突包** —— 会话分叉中
  落选的那个分支 —— 它位于 `semantic.root_dir` 下，永远不会被清理，因此分叉的历史不会
  只存在于一个会过期的地方。

## 还原一份备份

```powershell
codexsync -c config.toml backups list                                  # 列出快照及其名称
codexsync -c config.toml restore --dry-run                              # 预览，不写入任何内容
codexsync -c config.toml restore --apply                                # 把最新的备份还原到本地 .codex
codexsync -c config.toml restore --from <快照名称> --apply              # 指定某一份备份（目录或 .zip）
codexsync -c config.toml restore --target cloud --apply                 # 改为还原到云端镜像
```

- 还原需要 Codex 关闭，并且会先备份它将替换的一切。
- 试运行会逐个文件与备份清单核对。
- 没有清单的旧格式备份需要明确给出 `--from` 以及 `--allow-legacy-snapshot`，并且不能
  还原语义层拥有的状态（会话、会话索引、全局状态）。
- 0.1 做的备份没有清单，所以就是这种旧格式备份：只含设置文件。如果希望对话也能还原，
  请用这个版本重新做一份备份——同步在每次覆盖前都会做一份，而 `.codex` 副本
  （[配置](CONFIGURATION.md#codex-副本)）包含全部内容。

想从守护的快照把 `.codex-global-state.json` 写回去，见
[守护 → 还原一张快照](GUARDIAN.md#还原一张快照)。

## 被中断的写操作

`sync`、`restore`、`sessions apply`、`chats move`、`repair-projects apply`、
`project-move apply` 以及守护的还原，都会写一份可靠的事务日志。如果其中之一被中断 ——
断电、强制关机 —— 日志就会保持打开状态，而之后的每一次写入都会拒绝开始，直到它被收尾。
正是这道阻塞，防止一个只应用了一半的状态被继续改动。有两条命令可以收尾它。

**在替换任何文件之前就停止的操作会自动收尾。** 停在 `PREPARED` 或 `BACKED_UP` 的日志
证明没有任何文件被替换 —— 常见原因是云客户端在上传日志文件时占用了它，于是本该关闭日志的
那次写入也被拒绝。同一台电脑上的下一次写入会关闭这样的日志（历史里的原因是 *Abandoned*），
然后继续。仍然需要你来决定的有两种：已经进入提交阶段的操作（`COMMITTING`、
`RECOVERY_REQUIRED`），以及另一台电脑的操作 —— 日志文件夹位于共享的工作区里，那个操作
可能还在进行。

**查看哪些还开着**（没有副作用）：

```powershell
codexsync -c config.toml recover list           # 未关闭的日志，以及各自怎样收尾
codexsync -c config.toml recover list --all     # 包括已结束的；--json 供脚本使用
```

在窗口里，同一份列表就是 **「恢复」** 页面；被未关闭日志挡住的同步会提供
**「打开恢复页面」** 按钮，直接跳到那条日志。

**先读证据**（没有副作用）：

```powershell
codexsync -c config.toml recover inspect <操作标识>
```

**继续** —— 重新执行被中断的命令：

```powershell
codexsync -c config.toml recover resume <操作标识>          # 只报告
codexsync -c config.toml recover resume <操作标识> --apply
```

`resume` 不会重放丢失的计划。每个目标都是原子替换的，因此崩溃之后每个文件要么完全是旧的、
要么完全是新的，而重新执行原来的命令会按磁盘上的实际情况重新规划。`resume` 会确认该操作
的备份仍然完好，然后关闭日志，好让那条命令可以再跑一次。

**回滚** —— 还原该操作在写入任何内容之前创建的备份：

```powershell
codexsync -c config.toml recover rollback <操作标识>
codexsync -c config.toml recover rollback <操作标识> --apply
```

- 每个文件都回到它被备份时所在的一侧：一次同步会备份两侧的文件，而备份记录了每个文件
  属于哪一侧。`--target`（`local` 或 `cloud`）可以不给；给出时，它必须是备份中唯一的
  一侧，否则回滚会被拒绝，而不是把文件写进错误的根目录。只有当一次 `restore` 的备份写于
  开始记录侧别之前时才需要它；来自 `sync` 的这种备份会被拒绝 —— 请用 `restore --from`
  手动还原。
- 全局状态（在 `chats move`、`repair-projects apply`、`project-move apply` 或 Guardian
  还原之后）通过与原操作同样谨慎的写入放回：先对当前文件做经过校验的备份，再验证，再做
  进程检查。
- `sessions apply` 不做回滚：请用 `resume`，然后重新 scan 和 apply。一次传输只会延长一段
  历史，或者把被替换的分支保存在冲突包里，所以重做不会丢失任何东西。
- 两条命令都默认是试运行，并且都要求 Codex 关闭。
- `rollback` 会在释放日志之前检查一切可能导致拒绝的东西 —— 备份对照它已提交的清单、侧别、
  全局状态是否有效 —— 因此跑不起来的回滚会让阻塞原样保留。
- 如果日志指名的备份已经不在了（被保留期限删除，可能是共用备份文件夹的另一台机器删的），
  回滚会被拒绝：已经无法证明替换过什么。`resume` 仍然可以关闭日志。
- 从未进入提交阶段的日志，或者没有已提交清单的备份，都证明什么也没有被替换 —— 两者都在
  第一次替换之前记录 —— 所以没有什么需要撤销，日志直接关闭即可。
- 同一个 Codex 文件夹同一时间只能有一条会写入的命令在工作，不论它是哪一种；第二条会以
  退出码 `5` 停下，而不是把自己的写入和前一条交错在一起。

## Codex 无法启动时

```powershell
codexsync -c config.toml codex check
codexsync -c config.toml codex repair
codexsync -c config.toml codex repair --confirm-plan <plan-id>
```

`codex check` 读取 Codex 的状态并列出问题，无论是谁造成的；在窗口中是“恢复 → Codex 状态 →
检查 Codex”。检查只读取，可以在 Codex 打开时运行。每一项发现都写明了修复方法：

| 发现 | 含义 | 修复 |
|---|---|---|
| `CATALOGUE_REBUILD_STUCK` | Codex 无法启动：聊天列表的重建中途被中断，且没有进程在继续。Codex 显示“无法加载组织设置”。 | `codex repair` |
| `CATALOGUE_REBUILD_PENDING` | 下次启动时 Codex 会遍历所有聊天文件重建聊天列表；在聊天出现之前请保持打开。 | `codex repair` 跳过重建 |
| `CATALOGUE_REBUILDING` | Codex 正在重建。 | 保持打开 |
| `CATALOGUE_MISSES_CHATS` | 聊天文件在原位，但 Codex 没有显示。 | `sessions catalogue` |
| `GLOBAL_STATE_MISSING`、`GLOBAL_STATE_INVALID` | 项目、置顶或聊天所属项目丢失或已经脱节。 | `guardian restore` |
| `OPEN_JOURNAL` | 某个 codexSync 操作中途停止，阻止所有写入。 | [被中断的写操作](#被中断的写操作) |

重建为什么会卡住：Codex 在启动时根据聊天文件构建聊天列表，完成后才打开。如果这次启动被中途结束
——窗口放弃等待，或 Codex 被关闭——重建会一直标记为“进行中”，之后的每次启动都会等待它然后退出。
`codex repair` 把它设回“已完成”：只改一行，在 Codex 关闭时、数据库备份经过校验之后进行，并像
所有写入一样记录日志（类别 `codex-repair`）。如果重建是 codexSync 自己请求的，会放回当时备份
中的那一行。之后 Codex 会带着已列出的聊天启动；当你可以让 Codex 一直打开直到完成时，再用
`sessions catalogue` 请求重建。`doctor` 在 `codex_startup` 中报告同一状态。
