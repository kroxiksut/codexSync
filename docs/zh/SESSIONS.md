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
- **分叉** —— 两侧各自添加了不同的记录。

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --save-plan sessions-plan.json
```

除了计划文件之外它什么都不写，Codex 开着时也能运行，只是这样得到的计划会被标记为易变，
不能执行。报告里不出现会话标识、线程名称或记录内容：一处冲突只用它的标识来指称。

## 工作集

只需要一个项目的笔记本，不必把每个会话都带上：

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --project project-chloya --save-scope --save-plan sessions-plan.json
```

- `--project` 和 `--chat` 可以重复给出；`--scope-file` 读取之前保存的集合；
  `--save-scope` 把这次的集合按机器对保存在
  `plans/sessions-scope-<从>-<到>.json` 中。
- 一个项目意味着它的全部对话 —— 固定的、按路径找到的，或者靠某条 `[[path_mappings]]`
  规则连上的 —— 以及它们派生出的子线程，包括在选定集合之后才在另一台机器上出现的对话。
- **云端镜像始终接收全部会话**，因此备份永远不会变成残缺的。工作集只收窄写入 `.codex`
  的范围。
- **工作集是计划标识的一部分**，因此一份计划不可能在另一个工作集之下被执行。

被留下的会话会报告为 `OUT_OF_SCOPE`。它不阻止任何事，并且带着「本来会是什么」
（`WOULD_BE_…`），所以汇总会说明什么被留下了，而不是把它略去。

## 分叉

分叉永远不会被自动解决 —— 不交错、不按时间戳排序、也没有「新的赢」。两个分支都原样保留，
计划会被阻塞，直到记录下一个决定：

```powershell
codexsync -c config.toml sessions resolve --plan sessions-plan.json --conflict <冲突标识> --choice KEEP_LOCAL --output resolutions.json
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --resolutions resolutions.json --save-plan sessions-plan.json
```

`--choice` 可以是 `KEEP_LOCAL`、`KEEP_REMOTE` 或 `DEFER`。一个决定被钉在两个分支的
确切字节上：如果其中任何一侧之后发生变化，这个决定会以 `STALE_RESOLUTION` 被拒绝，
而不会被套用到你从未见过的历史上。

记录是否相同由原始字节决定。只有在可以证明毫无歧义的地方才会参考规范化 JSON，因此两条
不同的记录永远不可能被合并成一条。

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
- 在某个决定中落选的分支，还会保存在 `semantic.root_dir` 下一个不可变的**冲突包**中。
  备份会按保留期过期；正是这个冲突包保证了分叉的历史不会只存在于一个会过期的地方。
- 执行**按设计就是部分的**。冲突或目标位置冲突会让整份计划停下来，因为每一个都指向只有
  你才能做的决定。因布局未经验证或因 SQLite 目录而被挡住的条目，会被报告出来并原地不动。
- 活动 ↔ 归档的转变在 0.2 里只报告、不执行：这种搬移需要一次删除，而 codexSync 从不删除
  会话文件。

### 写入 `.codex`

Codex 运行时到哪里去找会话文件，是那个运行时自己的属性：把文件放到别处，会话就会
完全无声地消失。因此写*入* `.codex` 需要两样东西：

- **一个经过验证的目标布局。** 在受控实验把它记录下来之前，这样的条目会报告为
  `BLOCKED_UNPROVEN_LAYOUT`（[实验](../dev/experiments/session-layout-adapter.md)，英文）；
- **Codex 线程目录（`state_*.sqlite`）中恰好指向该文件的一行记录。** codexSync 不写
  SQLite，因此目录从未听说过的会话无法被显示出来，会报告为 `UNSUPPORTED_STATE_BACKEND`。

写**向云文件夹**不受这道关卡限制：没有任何 Codex 读取那份副本，因此分支保留它在本地的
相对路径。正是这一点，让过期或缺失的镜像可以被重建出来。

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
