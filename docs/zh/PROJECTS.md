# 项目与对话

[English](../en/PROJECTS.md) · [Русский](../ru/PROJECTS.md) · **中文**

[← 文档](README.md)

一个 Codex 项目就是一个文件夹（它的*根目录*）加上属于它的对话。在换机或者文件夹搬家
之后，路径就对不上了，于是对话会无声地不再出现在它的项目下面。本页讲的是怎么找到它们、
怎么固定它们、怎么修复一次换机，以及怎么搬移项目的文件。在窗口里，这对应
[对话归属](GUI.md#对话归属)和[项目](GUI.md#项目)两个页面。

> [!NOTE]
> Codex 同时还把项目放在 `state_*.sqlite` 里，而 codexSync 从不写入它。因此搬移项目或
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
codexsync -c config.toml chats move --chat 01a00ab4 --to LabTakt
codexsync -c config.toml chats move --chat 01a00ab4 --to LabTakt --confirm <计划标识>
```

- 这里没有计划文件：标识既覆盖各项决定，*也*覆盖读取它们时状态的确切字节，所以只要有
  任何变动，它立刻就不再匹配。
- 写入是每个对话一条绑定，形状按检测到的状态结构来，并且和其他任何写入走同一套流程：
  先做经过校验的完整备份，替换前紧接着重新检查进程，之后任何环节失败都会经过校验地回滚。
- `--dry-run` 会执行全部检查，并且不写入任何内容。

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
- **两个候选项**都映射到同一个新根目录时，会报告为 `AMBIGUOUS_PROJECT`，并且什么都不执行。
- **会话文件内部的任何内容都永远不会被修改。** 一条记录的原始字节就是它在分支比较中的
  身份，因此在里面改写 `cwd` 会让同一段历史在两台机器上永久分叉。

## 搬移项目的文件

`project-move` 在一台机器上完成搬移本身：

```powershell
codexsync -c config.toml project-move scan --project LabTakt --to D:/Work/labtakt --save-plan move.json
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
