# 项目与对话

[English](../en/PROJECTS.md) · [Русский](../ru/PROJECTS.md) · **中文**

[← 文档](README.md)

一个 Codex 项目就是一个文件夹（它的*根目录*）加上属于它的对话。在换机或者文件夹搬家
之后，路径就对不上了，于是对话会无声地不再出现在它的项目下面。本页讲的是怎么找到它们、
怎么固定它们、怎么修复一次换机，以及怎么搬移项目的文件。在窗口里，这对应
[对话归属](GUI.md#对话归属)和[项目](GUI.md#项目)两个页面。

> [!NOTE]
> Codex 同时还把项目放在 `state_*.sqlite` 里，而 codexSync 从不修改其中的项目记录。因此搬移项目或
> 重新指定根目录只会改写 `.codex-global-state.json` 中的根目录，删除或合并项目则根本
> 不提供。`doctor` 每次运行都会报告这一点
> （[实验](../dev/experiments/project-registry-contract.md)，英文）。

## 对话

`chats tree` 会打印出各个项目，把它们的对话列在下面，并把不属于任何项目的对话放在最后。
`chats list` 是同样的信息，只是带筛选。两者都只读取，Codex 开着时也能运行。

```powershell
codexsync -c config.toml chats tree
codexsync -c config.toml chats list --text "parser" --limit 20
codexsync -c config.toml chats list --project none
```

`chats list` 还接受 `--association`、`--since`/`--until`、`--sub-threads`
（智能体派生的线程默认隐藏）和 `--json`。

每一行都会说明这个对话*为什么*在这里，而三种原因的表现并不相同：

| 标记 | 归属 | 含义 |
|---|---|---|
| `pinned` | `BOUND` | 有一条明确的 `thread-project-assignments` 记录。项目路径变了它也跟着走。 |
| `by path` | `DERIVED` | 没有记录；对话记下的文件夹落在项目根目录之下。项目一搬走，它就被留在原地。 |
| `by rule!` | `DERIVED_VIA_MAPPING` | 这个文件夹写的是*另一台机器*上的路径，只有某条 `[[path_mappings]]` 规则把它和这里的项目连起来。Codex 不读这些规则，所以它会把这个对话显示为完全没有项目。 |

最后一种正是换机造成的：项目在笔记本上位于 `C:`，而从台式机来的每个对话记录的仍是 `D:`。
可以正好把这些列出来：

```powershell
codexsync -c config.toml chats list --source-machine desktop --target-machine laptop --association DERIVED_VIA_MAPPING
```

## 把对话移到某个项目

移动的第一步永远是预览。在你带着它打印出的计划标识再执行一次之前，`chats move` 什么都
不写；而真正写入时 Codex 必须关闭：

```powershell
codexsync -c config.toml chats move --chat 3f9c2e71 --to Atlas
codexsync -c config.toml chats move --chat 3f9c2e71 --to Atlas --confirm <计划标识>
```

- 这里没有计划文件：标识既覆盖各项决定，*也*覆盖读取它们时状态的确切字节，所以只要有
  任何变动，它立刻就不再匹配。
- 写入是每个对话一条绑定，形状按检测到的状态结构来，并且和其他任何写入走同一套流程：
  先做经过校验的完整备份，替换前紧接着重新检查进程，之后任何环节失败都会经过校验地回滚。
- `--dry-run` 会执行全部检查，并且不写入任何内容。

## 机器之间的项目

Codex 的侧边栏——项目、它们的顺序、哪些被置顶，以及显式绑定把哪些对话放在某个项目下——属于
`.codex-global-state.json`。这个文件里还有只属于一台电脑的内容（窗口位置、远程控制标识、迁移
标记），所以它从不被整体复制。每次完整同步只搬运其中的**项目部分**：

- 每台电脑把自己的项目列表发布到 `state.manifest_file` 旁边的 `projects` 文件夹（每台电脑
  一个可自校验的文件，只由它自己写入）；
- 下一台电脑把它**合并**进来：项目先按 id 匹配，再按文件夹匹配（经过
  [`[[path_mappings]]`](CONFIGURATION.md#path_mappings)）；本机没有的项目会按 Codex 在另一台
  电脑上写下的原样加入；
- **不会删除任何东西**——只有本机才有的项目保持原位；
- 对两台电脑都有的项目，置顶、顺序和对话绑定以另一台电脑为准，但只来自本机尚未接收过的
  列表，所以未变化的列表不会再次覆盖本机之后所做的修改；
- 文件夹在本机不存在的项目仍会连同对话一起被加入（`D-034`，见下文）；
- 侧边栏的**分区**也以同样方式搬运：分区、它的名称、其中的项目和单独对话及其顺序；另一台电脑
  放进某个分区的条目，会离开本机的其他分区，分区本身从不删除（`D-036`）。

它是 `handoff sync` 和窗口中**同步**按钮的一部分。手动执行：

```powershell
codexsync -c config.toml projects sync                        # 预览：将加入什么，计划 id
codexsync -c config.toml projects sync --confirm-plan <id>    # Codex 关闭：合并，然后发布本机的列表
```

写入与全局状态的其他任何修改走同一套流程：Codex 关闭、先做经过验证的备份，出错时可通过
[`recover`](RECOVERY.md#被中断的写操作) 回滚。Codex 还把项目保存在 `state_*.sqlite` 中，codexSync
从不修改其中的项目记录；侧边栏以 JSON 文件为准。

### 在另一种操作系统上

同一个项目文件夹在 Windows、Linux 和 macOS 上几乎不会是同一路径
（`D:\Projects\atlas`、`/opt/atlas`、`/Users/you/code/atlas`）。codexSync 只问一次它在
哪里，并记住答案（`D-033`）。在回答之前，项目照样会被带来（`D-034`）：

- 本机没有文件夹的项目会**连同它的对话**一起带来，对话可以阅读；不会有任何警告——本机不处理的
  项目本来就很常见。如果它的文件夹按另一种系统写法记录、且没有规则能在本机找到，它会原样
  保留那台机器上的路径（Codex 会显示这样的项目并打开它的对话）；
- 要在本机处理它的文件或继续它的对话，就为它指定文件夹——下一次同步会把项目改指向那里。在此之前
  Codex 可以打开它的对话来阅读，但在这里继续的对话会在一个不存在的文件夹中运行。可以在窗口中回答（**项目 → 其他机器的项目在本机的位置 → 选择本机的文件夹…**），也可以
  在控制台：

```powershell
codexsync -c config.toml projects places                                  # 本机没有文件夹的项目，附建议
codexsync -c config.toml projects place atlas --path /opt/atlas           # 它在这里
codexsync -c config.toml projects place old-tool --skip                   # 不带到本机
codexsync -c config.toml projects place atlas --forget                    # 忘记这个回答
```

- 答案保存在 `state.manifest_file` 旁边的 `path-places` 文件夹中，而不是 `config.toml`，
  并适用于**所有方向和所有机器**：另一台机器反向读取它；两台机器若各自为第三台机器的同一个
  文件夹指定了位置，它们之间也能互相对应。如果两端都以项目文件夹名结尾，上一级文件夹也会被
  记住（`D:\Projects` 就是 `/opt`），同一文件夹中的其他项目就无需再问（`--only-this` 关闭
  此行为）。两台机器都有、但位于不同文件夹的项目，本身就构成这样一对；
- 下一次同步会把项目加入到那里；之前带过来、但文件夹在本机不存在的项目，一旦新文件夹出现，
  就会改指向它；在本机选定的文件夹永远不会被改动；
- 对话随之而来：Codex 把大多数对话仅按其文件夹归入项目，因此没有绑定的对话会被绑定到它在
  来源机器上所在文件夹对应的项目。已有绑定的对话不会被改动；任何一台机器上 Codex 显示为“无项目”的对话
  也不会被改动：即使它的文件夹就是项目文件夹，它也保持为普通对话，直到你自己把它放进项目；
- 同一台机器上两个不同的文件夹若被规则映射到本机的**同一个**文件夹，两者都保持原样，它们的
  对话也一样——合并会把一个项目的对话放进另一个项目。

建议——与已有位置的项目同级、同名的文件夹——只会被提出，从不会被自动选定。路径是否区分
大小写取决于机器：macOS 的路径写法与 Linux 相同，但和 Windows 一样不区分大小写。

### 项目文件夹

对话和项目列表会随同步迁移，项目自身的文件却不会——它们在 git 里、在云盘文件夹里，或者只在一块磁盘上。
在旧版本代码上继续的对话会处理错误的文件，因此每次完整同步还会比较项目文件夹（`D-026`）。每台机器把
自己文件夹的情况发布到清单旁边的 `project-files` 中：git 文件夹记录提交、分支以及是否有未提交的更改；
其他文件夹记录每个文件的大小和 SHA-256（`node_modules`、`.venv` 之类的工具文件夹不计入；超过 100 MB
的文件按大小和时间比较）。只有大小或时间变化的文件才会重新读取——哈希缓存在每台机器上，位于工作区之外。
每个项目还附带它的聊天在那台机器上最后一次变化的时间。下一台机器比较所有项目（任何东西都可能改动文件夹），
并先提示在那边继续过聊天的项目：

- 另一台机器的最新提交不在本机——请拉取；
- 两台机器各自提交了不同的内容——请合并；
- 另一台机器上有未提交的更改，本机没有；
- 普通文件夹中有在另一台机器上较晚修改的文件、只有那边才有的文件，或那边已删除而本机仍有的文件——
  `projects files` 会列出它们；
- 本机没有该文件夹，或本机有而那边没有。

文件夹和文件随时可能出现或消失，因此不会沿用之前检查的结果：每次检查都重新读取文件夹并发布本机这一侧——
完整同步、`projects files`，以及窗口的**每次启动**；这样，尚未同步过的机器也会告诉其他机器它有什么。
某台机器上删除的文件会在它的发布中保留 90 天，以便下一台机器区分“那边删除”和“本机新增”。

不会复制、拉取或阻止任何东西，只存在于本机的工作不会触发提示。安装了 git 时才使用它，而且只读：
不会改动索引。窗口中的**项目 → 项目文件夹**列出每个没有完整迁移过来的项目及其下的文件，完整同步的结果中
也有按钮通往那里。`codexsync -c config.toml projects files` 打印同样的列表并包含所有文件——拉取之后运行它，
即可确认提示已消失（`--all` 列出所有项目）。

## 换机之后的修复

当项目文件夹搬了家 —— 改了名、放到另一个盘、或者在第二台机器上以不同的根目录打开 ——
Codex 就找不到它了。请用一条
[`[[path_mappings]]`](CONFIGURATION.md#path_mappings) 规则声明旧前缀现在对应哪里，
然后扫描：

```powershell
codexsync -c config.toml repair-projects scan --source-machine desktop --target-machine laptop --save-plan repair-plan.json
codexsync -c config.toml repair-projects apply --plan repair-plan.json --confirm-plan <计划标识> --dry-run
codexsync -c config.toml repair-projects apply --plan repair-plan.json --confirm-plan <计划标识>
```

计划是按会话实际记录的内容、经过映射规则转换之后构建出来的。试运行会执行真正执行时的
每一处拒绝判断，包括进程检查。

- **`REMAP_ROOT`。** 如果某个已有项目*记录在案*的根目录，经过同样的映射之后，正好落在
  会话现在指向的文件夹上，计划就会重新指定这个项目的根目录，而不是再造一个新项目。
  只有那一个路径值会被改写，因此项目保留它的标识和名称。
- **重新指定根目录从不单独出现。** 搬家之前创建的对话记录的是旧文件夹，把根目录从它
  移走会让这些对话消失。因此仍然位于旧根目录之下的每个会话，都会同时被固定到这个项目上。
  如果还有这样的对话没被覆盖到，计划会报告 `REMAP_ORPHANS_SESSIONS`，并且拒绝执行。
  旧根目录之下已经属于*另一个*项目的对话（嵌套项目，或是你把它移过去的项目）不会被抢走：
  计划会报告 `REMAP_SESSION_BOUND_ELSEWHERE`，在你决定它的归属之前拒绝执行。
- **文件夹在本机不属于任何项目的对话**会列为 `SKIP_NO_PROJECT`，保持原样。桌面版的项目
  条目带有 codexSync 不会凭空编造的字段，所以它从不在那里创建项目：请在 Codex 中创建
  项目后重新扫描。这样的对话不再阻塞计划的其余部分。
- **两个候选项**都映射到同一个新根目录时，会报告为 `AMBIGUOUS_PROJECT`，并且什么都不执行。
- **会话文件内部的任何内容都永远不会被修改。** 一条记录的原始字节就是它在分支比较中的
  身份，因此在里面改写 `cwd` 会让同一段历史在两台机器上永久分叉。

## 搬移项目的文件

`project-move` 在一台机器上完成搬移本身：

```powershell
codexsync -c config.toml project-move scan --project Atlas --to D:/Work/atlas --save-plan move.json
codexsync -c config.toml project-move apply --plan move.json --confirm-plan <计划标识> --dry-run
codexsync -c config.toml project-move apply --plan move.json --confirm-plan <计划标识>
```

1. 项目被复制到一个尚不存在的文件夹里。
2. 每个复制过去的文件都会重新计算哈希，与计划核对。
3. 校验无误的副本被重命名到位。
4. 只有到这一步，项目根目录才会被重新指定，靠路径归属到该项目的对话才会被固定。

**旧文件夹永远不会被修改或删除** —— 确认副本无误之后，删不删由你决定。

扫描会拒绝这些目标：已经存在的、位于项目内部（或者项目位于其内部）的、与 `.codex`、
镜像、备份、临时目录或快照存储重叠的、属于另一个项目的；同样也会拒绝包含符号链接、
目录联接或无法读取的文件的项目。云端占位文件（例如 Yandex.Disk 的）算普通文件，不是链接。
如果复制之后状态提交失败，校验无误的副本会保留下来，重新扫描时会认出它
（`copy_complete`），于是再跑一次只会更新 Codex。

复制不受 Windows 每条路径 260 个字符的限制，所以即使没有开启长路径支持，层级很深的 `.git`
也能搬过去。不过在这样的电脑上，如果新文件夹里最长的路径达到这个限制，扫描会指出来：不支持
长路径的程序可能打不开这样的文件。之前对同一次搬迁的失败尝试留下的副本，扫描会列出来，搬迁在
开始新的复制之前会把它删除。
