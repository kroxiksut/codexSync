# 会话

[English](../en/SESSIONS.md) · [Русский](../ru/SESSIONS.md) · **中文**

[← 文档](README.md)

一个 Codex 会话就是一个 JSONL 文件 —— 一段只会增长的历史。当同一个会话在两台机器上
继续下去，两份副本就成了同一段历史的两个*分支*。把较新的文件覆盖到较旧的文件上，会
悄无声息地丢掉另一台机器上新增的内容，因此会话永远不由普通的 `sync` 复制。在窗口里，
这就是[会话](GUI.md#会话)页面。

## 扫描

`sessions scan` 把本机的每个会话分支与云文件夹中的副本比较，并给每一个分类：

- **完全相同**；
- 任一方向的**快进** —— 一侧是另一侧的前缀，于是较长的那侧可以直接取代它；
- **活动 ↔ 归档的转变** —— 同一段历史在 `sessions/` 与 `archived_sessions/` 之间
  搬了位置；
- **分叉** —— 两侧各自添加了不同的记录；
- **无法使用的副本**（`BLOCKED_INVALID_BRANCH`）—— 无法读取、被截断，或同一个会话
  标识出现在两个文件里（`LOCAL_DUPLICATE_SESSION_ID`、`REMOTE_DUPLICATE_SESSION_ID`），
  或者副本会落到一个不属于该会话的文件上（`DESTINATION_OCCUPIED`）。这样的副本绝不会被
  当作不存在：在文件能被正常读取之前，这个会话的任何一侧都不会被写入，其他会话也不会因此停下。
  例外只有一个：如果 Codex 在新文件中继续了某个对话，而它的线程目录恰好指向两者之一，
  那个文件就是该对话，另一个保持不动（`STALE_DUPLICATE_BY_CATALOG`）。

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --save-plan sessions-plan.json
```

除了计划文件之外它什么都不写，Codex 开着时也能运行，只是这样得到的计划会被标记为易变，
不能执行。报告里不出现会话标识、线程名称或记录内容：一处冲突只用它的标识来指称。

## 工作集

只需要一个项目的笔记本，不必把每个会话都带上：

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --project project-orion --save-scope --save-plan sessions-plan.json
```

- `--project` 和 `--chat` 可以重复给出；`--scope-file` 读取之前保存的集合（文件不存在
  或不是已保存的集合时，扫描以退出码 `4` 停下，而不是丢掉这个集合）；
  `--save-scope` 把这次的集合按机器对保存在
  `plans/sessions-scope-<从>-<到>.json` 中。
- 一个项目意味着它的全部对话 —— 固定的、按路径找到的，或者靠某条 `[[path_mappings]]`
  规则连上的 —— 以及它们派生出的子线程，包括在选定集合之后才在另一台机器上出现的对话。
- **云端镜像始终接收全部会话**，因此备份永远不会变成残缺的。工作集只收窄写入 `.codex`
  的范围。
- **工作集是计划标识的一部分**，因此一份计划不可能在另一个工作集之下被执行。

被留下的会话会报告为 `OUT_OF_SCOPE`。它不阻止任何事，并且带着「本来会是什么」
（`WOULD_BE_…`），所以汇总会说明什么被留下了，而不是把它略去。一个一个会话都不覆盖的
工作集——比如一个还没有对话的项目——不会往 `.codex` 写入任何东西，并被标记为
`WORKING_SET_MATCHES_NOTHING`；镜像仍然会被完整写入。

工作文件夹在本机不存在的对话——通常是放在同步文件夹之外的项目——会被标记为
`CWD_ABSENT_HERE`，`sessions scan` 会在 `cwd_absent_here` 中统计这类对话的数量。
文件夹按 `[[path_mappings]]` 映射后的位置查找；两条规则结论不一致时，对话会被标记为
`CWD_MAPPING_AMBIGUOUS`，而不是去猜。这个标记不阻止任何事：它的用处是让你把这些项目
留在工作集之外，而不是等打开对话才发现。扫描之后才创建的文件夹会改变计划标识，
所以执行时会要求重新扫描。

## 分叉

分叉永远不会被合并 —— 不交错、不对记录排序。两份副本中保留一份，另一份在替换任何内容之前
被整体放入冲突包。保留哪一份由 [`conflict.policy`](SYNC.md#冲突) 决定
（[D-027](../dev/DECISIONS.md)，英文）：默认保留最后一条消息较晚的副本，也可以总是保留本机的
副本，或总是保留云端的副本（单次扫描可用 `--conflict-policy`）。这样的条目带有
`RESOLVED_BY_RULE`。在 `manual_abort` 之下，以及对于在同一时刻结束的两份副本
（`RULE_CANNOT_DECIDE`），计划会被阻塞，直到记录下一个决定：

```powershell
codexsync -c config.toml sessions resolve --plan sessions-plan.json --conflict <冲突标识> --choice KEEP_LOCAL --output resolutions.json
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --resolutions resolutions.json --save-plan sessions-plan.json
```

`--choice` 可以是 `KEEP_LOCAL`、`KEEP_REMOTE` 或 `DEFER`。`DEFER` 让冲突保持未决，而
未决的冲突会拒绝**整个** `sessions apply`，而不只是那一个会话 —— 只推迟那些你会在下一次
apply 之前决定的冲突。`sessions scan` 恰好在计划里含有这样的决定（冲突或目标撞车）时以
退出码 `2` 结束；只因布局未经验证或因 SQLite 目录而被阻止的会话，会被 apply 跳过，也不
改变退出码。

一个决定被钉在两个分支的
确切字节上：如果其中任何一侧之后发生变化，这个决定会以 `STALE_RESOLUTION` 被拒绝，
而不会被套用到你从未见过的历史上。
记录下的决定（包括 `DEFER`）总是优先于策略。在窗口中，“会话”页面可以按一条规则一次决定
一次扫描中的全部冲突（**一次决定全部冲突**），也可以逐个决定。

很长的聊天会由 Codex 在第二个文件 `rollout-…-<id>_<other id>.jsonl` 中继续，这个文件以同一个
聊天标识开头，并说明它从哪里接续（`history_base`）。聊天就是这一串文件：每个文件各自传输
（续页会显示 `HISTORY_PAGE`），第一个文件照旧继续增长，两者之间都不算冲突。

记录是否相同由原始字节决定。只有在可以证明毫无歧义的地方才会参考规范化 JSON，因此两条
不同的记录永远不可能被合并成一条。

### 当 Codex 改写了自己的会话

2026 年 9 月，Codex 桌面版把所有已有的会话文件改写成了新的记录格式：每条记录都有编号
（`ordinal`），消息被移到了其他字段，部分记录在改写中丢失——你撤销过的轮次、重复的
`session_meta` 记录、注入的指令。各文件的修改时间保持为旧值。在此之前写入的云端镜像
与其中的每个会话都不一致。

这类冲突会被标记为 `FORMAT_MIGRATION`，并附带 `NEWER_FORMAT_LOCAL` 或
`NEWER_FORMAT_REMOTE`（只有内容上的分叉会被这样标记，缺少共同基础的冲突不会）；只要有一侧仍是旧格式，`doctor` 就会给出警告（`session_format`）。
它仍然是冲突——两份副本并不是同一段历史——但一个决定即可覆盖全部：

```powershell
codexsync -c config.toml sessions resolve --plan sessions-plan.json --format-migrations --output resolutions.json
```

它为每个冲突保留新格式的副本，并像其他决定一样钉在字节上。带有
`OLDER_FORMAT_HAS_LATER_RECORDS` 标记的冲突不会被处理：那里的旧副本中有比新副本更晚的
记录，可能是升级前在别处完成的工作，因此需要单独的 `--conflict … --choice …`。在窗口中，
这是 **全部保留新格式** 按钮。

执行计划时，每份即将被覆盖的旧副本都会被压缩保存一次，放在 `semantic.root_dir` 下的
`superseded/` 中——那是丢失的记录唯一还存在的地方。与冲突包不同，胜出的副本不会再存一份。

## 执行计划

执行需要 Codex 关闭，还需要准确的计划标识。计划会先按当前状态重新构建，并且它的标识
必须仍然吻合，所以扫描之后的任何变化 —— 分支变长了、出现了新的冲突、某个决定过期了 ——
都会让执行被拒绝：

```powershell
codexsync -c config.toml sessions apply --plan sessions-plan.json --confirm-plan <计划标识> --dry-run
codexsync -c config.toml sessions apply --plan sessions-plan.json --confirm-plan <计划标识> --resolutions resolutions.json
```

- 一个分支是**整体**传输的：不会往目标上追加内容，也不会交错任何历史。来源只被读取；
  目标在被替换之前已经存在于一份经过校验的备份里。
- 在某个决定中落选的分支，还会保存在 `semantic.root_dir` 下一个不可变的**冲突包**中，
  位于 `conflicts/<冲突标识>`——也就是你做决定时用的那个标识——两份副本在提交前都会重新校验哈希。
  备份会按保留期过期；正是这个冲突包保证了分叉的历史不会只存在于一个会过期的地方。
- 执行**按设计就是部分的**。冲突或目标位置冲突会让整份计划停下来，因为每一个都指向只有
  你才能做的决定。因布局未经验证、因 SQLite 目录或因副本无法使用而被挡住的条目，会被报告
  出来并原地不动。
- 在一台机器上归档（或取消归档）的对话，在另一台机器上也会同样搬移（`D-023`）。哪一侧
  发生了变化，从本机关于上一次一致状态的记录中读取，绝不依据时钟；本机没有自己的记录时，
  跟随云端副本。对话被写到另一台机器存放它的位置，旧文件在已验证的备份之后、在同一个
  封装内被删除。写入 `.codex` 时，线程目录必须指向被搬移的文件。在一台机器上归档、在另一台
  上继续的对话需要你来决定（`ARCHIVED_AND_CONTINUED`）。在 Codex 更新之前，目录中的那一行
  仍指向旧位置；`doctor` 会在 `session_visibility` 中统计这类对话。

### 写入 `.codex`

Codex 运行时到哪里去找会话文件，是那个运行时自己的属性：把文件放到别处，会话就会
完全无声地消失。因此写*入* `.codex` 走两条路之一。

**本机已有的聊天**——也就是在另一台电脑上继续过的聊天——会覆盖写入它自己的文件
（`IN_PLACE`）。Codex 线程目录（`state_*.sqlite`）记录着这个文件，因此不需要选择路径，
也不必猜测布局：同一个聊天较新的副本（连同后续内容）替换较旧的副本，而较旧的副本会先
进入经过校验的备份。只有当目录为这个聊天记录的恰好就是这个文件，并且聊天在两台电脑上
同为活动或同为已归档时才允许。否则条目保持 `BLOCKED_UNPROVEN_LAYOUT`，并附上说明原因的
代码：`IN_PLACE_CATALOG_ABSENT`、`SESSION_NOT_IN_CATALOG`、`CATALOG_PLACES_ELSEWHERE`、
`CATALOG_UNREADABLE`、`IN_PLACE_STATE_CHANGES`、`IN_PLACE_ARCHIVE_FLAG_DIFFERS` 或
`IN_PLACE_CONTAINER`。

Codex 还在这个目录中保存每个聊天的标题、预览和时间，而 codexSync 从不写入它。因此在
Codex 刷新之前，聊天列表可能仍显示旧值；对话本身则从文件读取。

**本机从未有过的聊天**由一项设置决定：

```toml
[semantic]
new_chats = "same_path"   # same_path | keep_in_cloud
```

- `keep_in_cloud` 让它留在云端副本中，报告为 `BLOCKED_UNPROVEN_LAYOUT`：
  Codex 期望新聊天文件放在哪里，尚未经过[受控实验](../dev/experiments/session-layout-adapter.md)（英文）验证。
- `same_path`（默认）把它写入 `.codex`，路径与它在来源机器上相对于 `.codex` 的路径相同
  （`sessions/<年>/<月>/<日>/…` 或 `archived_sessions/…`），这正是 0.1 整体复制
  `sessions/` 时的做法。路径是相对的，所以用户名和盘符无关紧要。每个这样的条目都带有
  `NEW_CHAT_SAME_PATH`。该路径上已有的文件永远不会被覆盖（`DESTINATION_OCCUPIED`）；
  目录把该聊天放在别处或无法读取时，写入仍会被拒绝（`BLOCKED_UNSUPPORTED_BACKEND`）。

Codex 从它的线程目录列出聊天。它只根据聊天文件填充一次这个目录，之后自行维护。之后才写入的
文件——也就是来自另一台机器的每个聊天——不在目录中，Codex 也就不显示它。完整同步会统计并报告
这些聊天，但**不会**请 Codex 重建列表（`D-032`）。是否请求由你决定：`codexsync sessions catalogue
--confirm-plan <id>`，或在窗口中进入“恢复 → Codex 状态”。随后 codexSync 在对目录做了已验证的
备份之后，把一行状态恢复为 Codex 创建它时的值（`D-024`），Codex 在下次启动时会先遍历**所有**
聊天文件才打开——在聊天出现之前请不要关闭它。如果这次启动被中途结束，重建会一直标记为“进行中”，
Codex 将完全无法启动（显示“无法加载组织设置”）；`codexsync codex check` 能发现这一情况，
`codex repair` 可以修复——见 [Codex 无法启动时](RECOVERY.md#codex-无法启动时)。codexSync 从不自己
把聊天写入目录。`doctor` 在 `session_visibility` 中报告同一个数字；`not_listed=0` 表示所有聊天
都可见。

聊天的**名称**也不在其文件中：Codex 只把它保存在该目录里，所以迁移过来的聊天起初显示为第一条消息。
因此每台机器把自己聊天的名称发布到清单旁边的 `chat-names` 中，完整同步会把另一台机器的名称设置给
本机尚无名称的聊天——绝不覆盖本机起的名称（`D-025`）。Codex 要到下次启动才列出的聊天，会在那之后的
一次同步中获得名称。`codexsync sessions names` 显示计划，加 `--confirm-plan` 时单独执行同样的操作。

聊天的工作文件夹在本机上可能位于别处。文件不会因此被修改（记录的字节就是它的身份）；
请用 `[[path_mappings]]` 映射文件夹，或移动项目，见[项目](PROJECTS.md)。

写**向云文件夹**不受这道关卡限制：没有任何 Codex 读取那份副本，因此镜像中还没有的分支
保留它在本地的相对路径。镜像中已有的分支则在原处被改写，即使本地副本放在别处
（`MIRROR_PATH_KEPT`），因为同一个会话的第二个文件会让两者都从之后的每份计划中消失；
只有归档搬移才会改变它的位置（`MOVES_BRANCH`）。正是这一点，让过期或缺失的镜像可以被重建出来。

## 云端镜像

因为没有运行时读取镜像，分支可以在那里压缩存放：

```toml
[semantic]
root_dir = "${workspace_root}/semantic"   # 分支清单与冲突包
mirror_compression = "xz"                 # none | gzip | xz
```

- `xz` 能把真实的会话数据存成大约三分之一的体积。只有镜像受影响：写回 `.codex` 的分支
  永远是普通的 JSONL。
- **压缩是容器的属性，不是历史的属性。** 分支哈希、记录数和一切比较都取自解压之后的
  流，因此压缩过的镜像副本与普通的本地分支比较结果是 `IDENTICAL`。
- 这个设置只对镜像里还没有的分支生效。已经在镜像里的分支保留它原本的容器
  （`MIRROR_CONTAINER_KEPT`）：容器是文件名的一部分，而同一个会话有两个名字，会让目录
  把这个会话从此后的每一份计划中丢掉。
- 容器是计划标识的一部分，因此改动这个设置会让已有的计划失效，而不是在你已经给出的
  确认之下悄悄改变目标文件名。

## 会话索引

`session_index.jsonl` 是一个追加/更新式的流水账，而不是现存会话的清单：同一个标识可能
出现在好几行上，某个会话可能一行都没有，某一行也可能指向一个已经不存在的文件。这些都
不是错误，codexSync 也从不去「清理」它。

```powershell
codexsync -c config.toml sessions index
```

报告会显示两侧的索引各有什么，以及它们在哪里不一致。它只读取，Codex 开着时也能运行，
并且不出现任何会话标识或线程名称。

- 重复出现的标识有两种都说得通的读法 —— 最后一行胜出，或者 `updated_at` 最大的胜出 ——
  而它们的差别恰恰出现在时钟往回走过的时候。这会报告为 `REDUCTION_AMBIGUOUS`；`doctor`
  也带着同样的检查。
- 两侧对同一个会话持有不同的记录，是一次重命名分叉，属于需要决定的事，而不是合并。
- 在运行时对索引的读法还没有得到验证之前，任何索引都不会被改写
  （`UNPROVEN_CONSUMER_CONTRACT`，
  [实验](../dev/experiments/session-index-contract.md)，英文）。
