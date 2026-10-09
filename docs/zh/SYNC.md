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

**`sync` 同步什么**由 `[sync] scope` 决定。设为 `full`（默认）时，它就是窗口里的“同步”：
设置文件、对话和项目——与 [`handoff sync`](#在电脑之间交接工作) 是同一次运行，试运行会构建
其中每一个计划。设为 `settings` 时，只复制 `targets.include_roots` 下的文件，也就是
0.2 之前 `sync` 所做的全部。`--scope` 只对一次运行生效，`--direction`（`bidirectional`、`to_cloud`、`to_local`）则在一次运行中替代 `sync.direction`——“同步”按钮旁的「本次运行」下拉框作用相同。登录时的任务运行的是 `sync`，
因此遵循同一设置。

在 Codex 开着时构建的计划会被标记为 `volatile`，只是预览：`sync --apply` 会在写入的
那一刻重新构建它的计划。

## 同步哪些内容

```toml
[targets]
include_roots = ["sessions", "session_index.jsonl", "skills", "plugins"]

[filters]
exclude_globs = ["**/*.lock", "**/*.tmp", "**/*.temp", "**/tmp/**", "**/cache/**", "**/.cache/**", "**/__pycache__/**", "**/*.log", "skills/.system/**", "plugins/.plugin-appserver/**", "plugins/.remote-plugin-install-staging/**"]
```

- `include_roots` 是相对于 `.codex`（以及镜像）的路径。模板里列出了 `sessions` 和
  `session_index.jsonl`，但 `sync` 会跳过它们（见下）；搬运它们的是 `sessions`。
- `include_roots` 必须指向 `.codex` 内部的东西：空列表、`""` 或 `"."` 会以退出码 4
  被拒绝，因为它们意味着整个目录。没有这个键的配置可以加载（例如只用于守护），但
  `sync` 不会用它运行。
- 在 `exclude_globs` 里，`**` 表示任意多个路径段，`*` 和 `?` 在单个路径段内匹配，
  不含 `/` 的模式匹配任意深度处的同名文件。
- **语义层拥有的路径永远不由 `sync` 复制：** `sessions/`、`archived_sessions/`、
  `session_index.jsonl`、全局项目状态和 SQLite。按修改时间复制它们可能丢掉在两台机器
  上都增长过的历史，因此改由 [`sessions`](SESSIONS.md)、[守护](GUARDIAN.md) 和
  [`repair-projects`](PROJECTS.md) 来处理。
- `skills/.system/**` 被排除：这些技能由 Codex 运行时自己安装和删除。在
  `delete_policy = "never"` 下，它删掉的文件会在下一次运行时从镜像里恢复，然后再被
  删掉——因此这棵子树完全交给运行时。
- **已编译的程序永远不会被同步**，无论 `include_roots` 和 `exclude_globs` 怎么写：
  Windows 的 `.exe`/`.dll`、Linux 的 ELF、macOS 的 Mach-O。按文件头识别，因为在
  macOS 和 Linux 上程序通常没有扩展名。程序只为一个平台、一个版本构建，会过时，
  安装程序可以恢复它；来自另一台机器的副本在那里毫无用处，或者会用旧版本替换新版本。
- `plugins/.plugin-appserver/**` 和 `plugins/.remote-plugin-install-staging/**`
  同样被排除：Codex 在那里存放它自己的程序——`codex.exe`、command runner、沙箱安装程序——
  按它自己的版本和平台构建。在 Mac 上它们毫无用处，在另一台 Windows 电脑上还会覆盖那里
  安装的版本。插件本身位于 `plugins/cache/`，已被 `**/cache/**` 排除：Codex 会按自己的
  配置重新安装它们。
- 存放凭据的文件（`auth.json`、`cap_sid`、`.sandbox-secrets` 之类）永远不会被复制，
  无论位于哪个根下的哪一层；「设置」页面也永远不会把它们列出来供你加入同步。
- `sync.session_mode = "last_date_only"` 会被拒绝：它可能丢掉分支。

## 文件如何比较

上一次运行的结果保存在一份清单里（`state.manifest_file`），它同时记录两侧的情况。
正是靠它，才能把「一侧发生了改动」和「冲突」区分开。

清单位于共享的工作目录里，因此它为**每台机器**各保存一条记录（`identity.machine_id`，
没有时用主机名）：这台机器上次同步时在两侧看到的情况。只有一条共享记录时，一台机器会把
另一台机器的上次同步当成自己的，把自己较旧的文件当作本地改动，并用它覆盖云端较新的
版本。旧版本写出的清单没有按机器区分的记录；其中的条目不归属任何机器，所以升级后的
第一次运行按首次同步处理——较新的文件胜出，只在一侧存在的文件被复制，不删除任何东西——
并记下这台机器自己的基线。

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

自上次运行以来两侧都被改过的文件或聊天就是冲突。同一条规则裁决这两者，并适用于每一次
运行——窗口、控制台和登录时的任务（[D-027](../dev/DECISIONS.md)，英文）。`conflict.policy`：

| 策略 | 会发生什么 |
|---|---|
| `prefer_newer_mtime`（默认） | 保留较新的副本：文件按修改时间，聊天按最后一条消息的时间 |
| `prefer_local` | 保留本机的副本 |
| `prefer_cloud` | 保留云端的副本 |
| `manual_abort` | 报告冲突，并在写入任何内容之前停止（退出码 `2`） |

没有被保留的副本永远不会丢失：文件在被覆盖之前会进入经过校验的备份，聊天则会整体放入
`semantic.root_dir` 下的冲突包（见[会话](SESSIONS.md#执行计划)）。聊天永远不会被合并：
两份副本中只保留一份。在同一时刻结束的两个聊天无法按时间排序，所以 `prefer_newer_mtime`
仍会就它们询问你；而你在“会话”页面为某个聊天记录的选择总是优先于这条规则。

对于单次运行，`sync`、`handoff sync` 和 `sessions scan` 的 `--conflict-policy` 会替代
这项设置：

```powershell
codexsync -c config.toml handoff sync --conflict-policy prefer_local
```

在窗口中，因冲突而停止的同步会在停下的地方提供同样的选择——**保留较新的版本**、
**保留本机的版本**、**保留云端的版本**——而 **以后都这样决定** 会把它保存为 `conflict.policy`。

策略没有裁决的冲突——`equal_mtime_action = "manual_abort"` 下时间相同、有争议的删除——
在任何策略下都会在写入之前以退出码 `2` 停止运行；只有字母大小写不同的两个路径
（`Rules/a.md` 与 `rules/a.md`）也是如此，因为不区分大小写的卷把它们存成同一个文件。

## 方向

`sync.direction` 决定哪一侧可以被写入（[D-012](../dev/DECISIONS.md)，英文）：

- `bidirectional`（默认）—— 两侧都写；
- `to_cloud` —— 只写云文件夹，本地的改动原样不动；
- `to_local` —— 反之亦然。

单向运行**不会**把它跳过的那一侧记录为「已同步」：对于它没有处理的每个路径，清单都
保留先前的记录，因此下一次双向运行仍然看得见差异，而不会得出两侧已经一致的结论。
文件冲突仍然是冲突，由 `conflict.policy` 决定。对聊天而言，单向本身也就是决定：
`to_cloud` 对两台电脑上都改过的聊天保留本机的副本，不向 `.codex` 写入任何内容；`to_local`
保留云端的副本，不向云端副本写入任何内容。`validate`、`doctor` 和计划报告都会说明
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
- 如果某个 `include_roots` 根在文件缺失的那一侧一个文件都没有，那么从这个根里的删除
  会成为冲突而不是删除：那一侧空的或缺失的文件夹（云文件夹正在重新下载、磁盘已断开）
  不应清空另一侧的同一个文件夹；
- 语义层拥有的路径永远不会以这种方式被删除。

## 在电脑之间交接工作

codexSync 所针对的流程是：在工作过的电脑上关闭 Codex，等待云端送达，在下一台电脑上
同步，然后才在那里启动 Codex。**交接**完成这两次同步，并在两者之间检查送达情况：

```toml
[handoff]
root_dir = "${workspace_root}/handoff"   # 可选：留空即 state.manifest_file 旁边的 handoff 文件夹
enabled = true                           # 下面的监视器任务
delivery_wait_minutes = 15
notify = true
```

```powershell
codexsync -c config.toml handoff status                   # 谁在工作、交接了什么、什么已到达
codexsync -c config.toml handoff status --check-delivery  # 同时对云端副本计算哈希
codexsync -c config.toml handoff sync                     # 先载入，再交接；Codex 必须已关闭
codexsync -c config.toml handoff watch                    # 登录任务运行的命令
```

交接是一次无人做决定的完整同步：设置按 `sync --apply --unattended` 的方式同步，
聊天按 `sessions apply` 的方式同步，并且先检查聊天计划。冲突——文件的和聊天的——由
[`conflict.policy`](#冲突) 裁决；**策略未能裁决的冲突会在第一次写入之前停止交接**，聊天永远
不会被合并。只有两部分都完成后，电脑才会记录这次交接。

每台电脑在 `root_dir` 中保存一个小文件：它是在*工作*（看到 Codex 正在运行）还是已*交接*，
最后一次交接的 id，交接时云端副本的指纹（每个文件的大小和 SHA-256，以路径的哈希为键——
不写入任何路径或聊天 id），以及它已载入其他每台电脑的哪次交接。交接是否已载入由其 id
决定，从不比较两台电脑的时钟；时间只是供你阅读。

**监视器**（`enabled = true`）是一个登录时启动、直到注销前一直运行的任务：

- 启动时如果 Codex 已关闭，它会载入另一台电脑交接的内容。如果云客户端尚未全部送达，它最多
  等待 `delivery_wait_minutes`，然后放弃并告知你——只到达一半的内容不会被载入；
- Codex 启动时，它把本机标记为工作中，如果另一台电脑仍在工作或其交接尚未在此载入，就会提醒你；
- Codex 关闭时，它进行交接。

它取代“登录后同步”：同时开启 `handoff.enabled` 和 `scheduler.sync_at_login` 属于配置错误。
它会用系统通知（`notify`）报告所做的事，并且总是写入日志。带着打开的 Codex 关机的电脑会在
下次登录时交接。

`--accept-undelivered` 会载入一直未完整到达的交接——只有在你知道原因时才使用，例如它所列出的
某个文件之后在云端副本中被手动修改过。

**聊天。**两台电脑都有的聊天——也就是在另一台电脑上继续过的聊天——会覆盖它自己的文件
载入 `.codex`，路径正是 Codex 自己的目录为它记录的路径。在另一台电脑上新开的聊天也会载入，路径与它在
那台电脑上的相同（`[semantic] new_chats = "same_path"`，默认值）；设为 `keep_in_cloud` 时它留在云端副本中（见[会话](SESSIONS.md#写入-codex)）。交接会说明
它把多少新聊天写入了 Codex、有多少聊天文件 Codex 还没有列出，以及有多少聊天仅保留在云端副本中，
而不是声称工作已载入。它从不请 Codex 重建聊天列表（`D-032`）：当你可以让 Codex 一直打开到重建
完成时，由 `sessions catalogue` 或“恢复 → Codex 状态”发出请求。

**项目。**最后合并项目列表：另一台电脑上的项目会加入本机，不会删除任何东西，两台电脑都有的
项目的置顶和顺序以另一台电脑为准。参见[项目 → 机器之间的项目](PROJECTS.md#机器之间的项目)。
窗口中的**同步**按钮运行的就是这同一次完整同步。

## 历史

每一次真正的同步都会留下一条操作日志，历史就是按从新到旧读取的这些日志——不需要另外维护记录：

```powershell
codexsync -c config.toml history                  # 最近 20 次各类运行
codexsync -c config.toml history --family sync    # 仅一种：sync、sessions、project-sync、chats、restore…
codexsync -c config.toml history --json --limit 0
```

一次完整同步是三次运行，每个部分一次：`sync`（设置文件）、`sessions`（聊天）和 `project-sync`（项目）。
每次运行都会显示开始时间、结果（失败时显示错误类型——绝不显示错误消息，因为其中可能含有文件名）、
启动方（`window`、`cli`、登录时任务对应的 `unattended`，或交接监视器对应的 `handoff`）、它传输了什么——
传到云端、传到本地以及被删除的文件或聊天，或新增的项目——以及它创建的备份。在这些字段出现之前写入的日志只显示总数。

不会列出：不写入任何内容的试运行，以及在第一次写入前就停止的运行——Codex 已打开，或策略未能裁决的
冲突。窗口在“同步”页面的 **历史** 选项卡中显示同一份列表，并在“概览”中显示最近一次运行。
