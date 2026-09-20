# 同步

[English](../en/SYNC.md) · [Русский](../ru/SYNC.md) · **中文**

[← 文档](README.md)

`sync` 在本地 `.codex` 与它在云文件夹中的镜像之间复制文件。它只在 Codex 关闭时运行，
并且在第一次替换之前，会为它将要替换的每个文件写出一份经过校验的备份。在窗口里，
这就是[同步](GUI.md#同步)页面。

## 命令

```powershell
codexsync -c config.toml plan              # 一次同步会复制什么；Codex 开着也能用
codexsync -c config.toml -v plan           # 同上，外加看到的 Codex 进程
codexsync -c config.toml sync --dry-run    # 同步的全部检查，不写入任何内容
codexsync -c config.toml sync --apply      # 真正的同步
```

不带参数的 `sync` 遵循 `sync.dry_run_default`（模板中为 `true`），因此真正的同步
始终需要 `--apply`。

在 Codex 开着时构建的计划会被标记为 `volatile`，只是预览：`sync --apply` 会在写入的
那一刻重新构建它的计划。

## 同步哪些内容

```toml
[targets]
include_roots = ["sessions", "session_index.jsonl", "skills", "plugins"]

[filters]
exclude_globs = ["**/*.lock", "**/*.tmp", "**/*.temp", "**/tmp/**", "**/cache/**", "**/.cache/**", "**/__pycache__/**", "**/*.log", "skills/.system/**"]
```

- `include_roots` 是相对于 `.codex`（以及镜像）的路径。模板里列出了 `sessions` 和
  `session_index.jsonl`，但 `sync` 会跳过它们（见下）；搬运它们的是 `sessions`。
- 在 `exclude_globs` 里，`**` 表示任意多个路径段，`*` 和 `?` 在单个路径段内匹配，
  不含 `/` 的模式匹配任意深度处的同名文件。
- **语义层拥有的路径永远不由 `sync` 复制：** `sessions/`、`archived_sessions/`、
  `session_index.jsonl`、全局项目状态和 SQLite。按修改时间复制它们可能丢掉在两台机器
  上都增长过的历史，因此改由 [`sessions`](SESSIONS.md)、[守护](GUARDIAN.md) 和
  [`repair-projects`](PROJECTS.md) 来处理。
- `skills/.system/**` 被排除：这些技能由 Codex 运行时自己安装和删除。在
  `delete_policy = "never"` 下，它删掉的文件会在下一次运行时从镜像里恢复，然后再被
  删掉——因此这棵子树完全交给运行时。
- 「设置」页面永远不会把存放凭据的文件（`auth.json` 之类）列出来供你加入同步。
- `sync.session_mode = "last_date_only"` 会被拒绝：它可能丢掉分支。

## 文件如何比较

上一次运行的结果保存在一份清单里（`state.manifest_file`），它同时记录两侧的情况。
正是靠它，才能把「一侧发生了改动」和「冲突」区分开。

`sync.compare`：

- `mtime`（默认）—— 比较大小和修改时间。
- `mtime_hash_fallback` —— 同样的快速路径，但当两边时间相同或接近
  （在 `sync.time_tolerance_seconds` 之内）时，比较内容的 SHA-256。

`sync.equal_mtime_action` —— 时间相同但文件不同时怎么办：

- `skip`（默认）—— 什么都不复制；
- `prefer_local` —— 把本地文件复制到云端；
- `prefer_cloud` —— 把云端文件复制到本地；
- `manual_abort` —— 当作冲突处理。

## 冲突

自上次运行以来两侧都被改过的文件就是冲突。`conflict.policy`：

| 策略 | 会发生什么 |
|---|---|
| `manual_abort`（默认） | 报告冲突，并在写入任何内容之前停止（退出码 `2`） |
| `prefer_cloud` | 采用云端版本 |
| `prefer_local` | 采用本地版本 |
| `prefer_newer_mtime` | 采用修改时间较新的一侧 |

## 方向

`sync.direction` 决定哪一侧可以被写入（[D-012](../dev/DECISIONS.md)，英文）：

- `bidirectional`（默认）—— 两侧都写；
- `to_cloud` —— 只写云文件夹，本地的改动原样不动；
- `to_local` —— 反之亦然。

单向运行**不会**把它跳过的那一侧记录为「已同步」：对于它没有处理的每个路径，清单都
保留先前的记录，因此下一次双向运行仍然看得见差异，而不会得出两侧已经一致的结论。
冲突仍然是冲突，由 `conflict.policy` 决定。`validate`、`doctor` 和计划报告都会说明
当前的方向。

## 删除

`sync.delete_policy` 决定删除是否会传播（[D-013](../dev/DECISIONS.md)，英文）：

- `never`（默认）—— 一侧缺失的文件会从另一侧复制回来。
- `propagate` —— 在另一侧也删除它，但仅限上一份清单能证明两侧都曾有这个文件、并且
  留存的一侧此后没有变化的情况。其他任何情况都算冲突。

在 `propagate` 之下：

- 删除之前会写出一份经过校验的备份，删除本身会单独记入日志；
- 它与其他任何写入一样，跑在同一套带日志的流程里，因此
  [`recover`](RECOVERY.md#被中断的写操作) 可以撤销它；
- 试运行不会删除任何东西；
- 刚打开这个选项之后的第一次运行不会删除任何东西，因为还没有证据；
- 语义层拥有的路径永远不会以这种方式被删除。
