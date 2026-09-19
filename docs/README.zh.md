# codexSync 文档

[English](./README.md) · [Русский](./README.ru.md) · **中文**

[← 项目主页](../README.zh.md)

这个目录里是三种语言的用户文档，以及面向开发者的文档。每种语言都是同样的八个
页面；面向开发者的文档只有英文。

## 用户文档

| 页面 | 内容 |
|---|---|
| [概览](zh/README.md) | 它如何工作、安装、首次运行、设计原则、平台 |
| [窗口](zh/GUI.md) | 全部十一个界面及截图 |
| [命令行](zh/CLI.md) | 每条命令、全局选项、退出码 |
| [配置](zh/CONFIGURATION.md) | 逐节讲解 `config.toml`，以及自动化 |
| [同步](zh/SYNC.md) | `plan` 与 `sync`：比较、冲突、方向、删除 |
| [快照守护](zh/GUARDIAN.md) | 全局状态的快照、隔离区、还原、新的基准 |
| [会话](zh/SESSIONS.md) | 跨机器的会话分支、工作集、镜像、索引 |
| [项目与对话](zh/PROJECTS.md) | 对话归属、换机之后的修复、搬移项目 |
| [备份与恢复](zh/RECOVERY.md) | 备份、还原、被中断的写操作 |

其他语言的同一批页面：[English](en/README.md) · [Русский](ru/README.md)。

## 面向开发者的文档

[docs/dev/](dev/README.md)，只有英文：代码所引用的编号
[决策](dev/DECISIONS.md) `D-001…`、[发布清单](dev/PUBLISHING.md)，以及
[实验](dev/experiments) —— 每个实验都是打开一道今天被刻意留空的闸门的唯一途径。

## 截图

`screenshots/<语言>/` 存放上面各页面引用的窗口截图。它们由
`scripts/docs_screenshots.py` 基于虚构的演示数据生成，绝不来自真实配置。
