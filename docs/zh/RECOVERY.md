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
- 备份按 `retention_days` 和 `max_backups` 清理。唯一的例外是**冲突包** —— 会话分叉中
  落选的那个分支 —— 它位于 `semantic.root_dir` 下，永远不会被清理，因此分叉的历史不会
  只存在于一个会过期的地方。

## 还原一份备份

```powershell
codexsync -c config.toml restore --dry-run                              # 预览，不写入任何内容
codexsync -c config.toml restore --apply                                # 把最新的备份还原到本地 .codex
codexsync -c config.toml restore --from <快照名称> --apply              # 指定某一份备份（目录或 .zip）
codexsync -c config.toml restore --target cloud --apply                 # 改为还原到云端镜像
```

- 还原需要 Codex 关闭，并且会先备份它将替换的一切。
- 试运行会逐个文件与备份清单核对。
- 没有清单的旧格式备份需要明确给出 `--from` 以及 `--allow-legacy-snapshot`，并且不能
  还原语义层拥有的状态（会话、会话索引、全局状态）。

想从守护的快照把 `.codex-global-state.json` 写回去，见
[守护 → 还原一张快照](GUARDIAN.md#还原一张快照)。

## 被中断的写操作

`sync`、`restore`、`sessions apply`、`chats move`、`repair-projects apply`、
`project-move apply` 以及守护的还原，都会写一份可靠的事务日志。如果其中之一被中断 ——
断电、强制关机 —— 日志就会保持打开状态，而之后的每一次写入都会拒绝开始，直到它被收尾。
正是这道阻塞，防止一个只应用了一半的状态被继续改动。有两条命令可以收尾它。

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
codexsync -c config.toml recover rollback <操作标识> --target cloud
codexsync -c config.toml recover rollback <操作标识> --target cloud --apply
```

- `--target`（`local` 或 `cloud`）必须给出，永远不会被推断：一次同步可能备份了两侧的
  文件，而备份清单只记录相对路径，因此仅凭备份无法证明是哪一侧。
- 两条命令都默认是试运行，并且都要求 Codex 关闭。
- `rollback` 只有在备份对照它已提交的清单校验通过之后才会释放日志，因此跑不起来的回滚
  会让阻塞原样保留。
- 没有已提交清单的备份，恰恰证明提交阶段从未开始 —— 备份集是在第一次替换之前盖章的 ——
  所以没有什么需要撤销，日志直接关闭即可。
