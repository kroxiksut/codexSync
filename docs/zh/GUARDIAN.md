# 快照守护

[English](../en/GUARDIAN.md) · [Русский](../ru/GUARDIAN.md) · **中文**

[← 文档](README.md)

Codex 把它的项目和对话绑定放在 `.codex-global-state.json` 里。如果在写这个文件的过程中
Codex 崩溃、机器断电或者蓝屏，它可能变成被截断的、甚至空的文件 —— 所有项目随之一起
消失。

守护是 codexSync 中唯一**在 Codex 开着时**运行的部分。它只读取这个文件，并把经过校验的
快照写进自己的目录，而配置必须让这个目录位于 `.codex`、镜像、备份和临时目录之外。
在窗口里，这就是[快照守护](GUI.md#快照守护)页面。

## 命令

```powershell
codexsync -c config.toml guardian snapshot --once   # 立即拍一张快照
codexsync -c config.toml guardian watch             # 在 Codex 运行期间持续拍摄
codexsync -c config.toml guardian list              # 快照与隔离区；不写入任何内容
```

想按计划自动拍摄，请设置 `[scheduler] mode = "guardian_snapshot"` 并运行
`automation apply`（见[自动化](CONFIGURATION.md#自动化)）。

## 一张快照是怎么决定的

- **稳定读取。** 文件必须连续多次读到相同内容（`stable_reads`，默认 3 次），这样在
  写入中途被捕捉到的状态永远不会成为快照。`watch` 每 3 秒轮询一次，变化之后等待 2 秒，
  并且每 60 秒完整重扫一遍，以防漏掉某次变化。
- **校验。** 字节、JSON 和引用完整性都会检查：NUL 字节、BOM、重复的键、指向不存在
  项目的绑定。
- **结构是被识别出来的，不是假定的。** 这个文件每一种已知的形态都有自己的适配器；
  没有任何适配器认得的状态不会被信任。
- **可疑的减少。** 与最近一张可信快照相比丢了很多项目或绑定的状态，会进入**隔离区**，
  而不是取代它（`shrink_min_count`、`shrink_ratio`）。
- **顺序与可见性。** 快照按单调递增的代次排序，而不是按时钟。一张快照只有在
  `COMMITTED` 之后才可见，任何可疑的东西都永远不可能成为 `latest-good`。

一个诚实的限制：按计划执行的 `snapshot --once` 并不能保证蓝屏发生的那一刻快照是最新的。
它保证的是：已经存在的那张快照是完整且可还原的。

## 还原一张快照

把快照写回去，第一步永远是预览。预览会说明状态现在有多少项目和绑定、之后会变成多少。
还原需要 Codex 关闭，还需要准确的计划标识，并且通过与其他任何全局状态改动相同的流程
写入：操作锁、事务日志、对被替换文件所做的、经过校验的备份、最后一次进程检查，以及
在替换之后任何环节失败时、经过校验的回滚。

```powershell
codexsync -c config.toml guardian restore --snapshot <快照标识>
codexsync -c config.toml guardian restore --snapshot <快照标识> --confirm <计划标识> --dry-run
codexsync -c config.toml guardian restore --snapshot <快照标识> --confirm <计划标识>
```

会被拒绝的情况：快照已经通不过校验、快照属于另一台机器、快照的结构与 Codex 今天写出的
文件不同，以及在实际文件缺失时进行还原。

## 接受新的基准

守护把每个状态都与 `latest-good` 比较。当项目或绑定的减少是**真实的** —— 比如 Codex 以
新的标识重建了它的项目 —— 之后的每个状态仍然会与减少之前的那个基准比较，于是
`latest-good` 再也不会前进。发生这种情况时 `doctor` 会发出警告。

`guardian accept` 只用数字来解释这次减少：多少项目以新标识重建、多少被删除、多少是新增的，
以及丢失的绑定分别指向其中的哪些。加上 `--confirm` 之后，它会把当前状态提交为
`latest-good`。

```powershell
codexsync -c config.toml guardian accept
codexsync -c config.toml guardian accept --confirm <计划标识>
```

- 只能接受「减少」，绝不能接受通不过校验的状态。
- 计划标识钉住的是这次减少，而不是文件的字节，因此即使 Codex 不断改写该文件，也仍然
  可以确认。
- 这张快照会被标记为 `SHRINK_ACCEPTED`，并且保留策略会同时保留它和被它取代的基准
  （[D-014](../dev/DECISIONS.md)，英文）。

## 设置

```toml
[guardian]
root_dir = "${workspace_root}/guardian"   # 位于 .codex、镜像、备份和临时目录之外
max_state_bytes = 67108864                # 接受的最大状态（1 MiB – 1 GiB）
shrink_min_count = 2                      # 丢失数量达到这么多时，减少才算可疑……
shrink_ratio = 0.25                       # ……并且比例要达到这么多
retention_days = 30                       # 0 表示不限
max_snapshots = 100                       # 0 表示不限
quarantine_retention_days = 30
staging_retention_hours = 24
poll_interval_seconds = 3
debounce_seconds = 2
stable_reads = 3
stable_read_interval_seconds = 0.5
fallback_scan_seconds = 60
once_timeout_seconds = 120
```
