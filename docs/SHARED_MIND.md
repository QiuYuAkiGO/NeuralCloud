# Misty 的共享运行规则

正常启动只有一个 Misty：本地聊天和 QQ 共用 Cognitive 的人格、模型连接、认知命令账本及长期记忆门面；Runtime 负责排队、接收序号和内稳态心跳；Homeostasis 独占一个持久状态目录。QQ 保留身体能力协商、唯一 GUI 执行器、两阶段 speak/wait 与人格回复，以及发送回执核验。

Linux 留言板也是同一 Misty 的身体能力。它通过 `expression.board.read/append/reconcile.v1` 和证据能力协商，只负责持久保存表达；不会启动另一个认知、模型或内稳态。入口为本地界面 `/board`，部署说明见 [Linux 留言板](../tools/board/README.md)。

进程锁按 `misty/cognition`、`misty/runtime`、`misty/homeostasis` 加锁。改变数据库路径不能启动第二个正常实例。共享服务不可用时请求失败，不回退到另一套脑或把故障当作自然沉默。

## 正常使用

已配置本机共享路径后，在仓库根目录运行：

```powershell
.\start.ps1 -Mode live
.\tools\qq\Start-QQSession.ps1
.\tools\qq\Start-QQWatcher.ps1
```

聊天和只读调试界面位于 <http://127.0.0.1:8790>。Runtime、Cognitive、Homeostasis 分别使用本机 8770、8771、8772；8789 为 QQ 的身份与观察接口。QQ 自主回复仍遵守已授权群、原有 10/60 秒频率限制和原消息截止时间。内稳态驱动的主动开口仍由 QQ 的机会、安静时间和身体状态共同判断，5 秒心跳不会每次触发发言。

`data/shared-misty-launch.json` 保存本机路径绑定，不存密钥正文。正常启动复用私有 token 文件；配置缺失、冲突或另一个 owner 已运行时明确拒绝启动。QQ watcher 的 `shared_control_token_file` 必须指向同一个 Runtime 控制 token。正常模式为 `study_runtime`，它与模型后端的 live/demo 选项不同。

省略 `-Mode` 时，正常入口沿用已保存的模型后端；`dev.ps1` 和 launcher 也不会隐式覆盖成 demo。显式参数优先。没有现存模型配置时才保留初始 demo 默认值。

暂停群聊：

```powershell
.\tools\qq\Stop-QQWatcher.ps1
```

确认 watcher 已暂停后，停止共享服务：

```powershell
.\stop.ps1
```

## 记忆与来源

QQ 资料、成员发言、已验证的发送回执进入共享持久记忆，保留群、成员数字身份、原始记录与证据哈希。本地已提交的输入和实际交付回复进入同一门面。生成中的草稿不是已发送消息，本地询问 QQ 历史也不会推进 QQ 的“已处理消息”账本。

共享不改变可见范围：QQ 记录可供本地所有者回忆；本地私聊默认对群不可见，只有显式授予该群可见范围的记录才能进入群上下文。群和成员不会因同名而合并。外部历史以用户资料上下文传给模型，不能成为系统指令。界面的共享记忆区可只读查看来源。

操作者明确要求记住的事实可通过现有 `SharedLocalEvent` 持久保存到 `operator-confirmed-facts` 来源会话，保留真实确认原文、时间与来源引用。共享认知为本地 `local:main` 的此类输入提供单独的有界检索；授予具体群可见范围后，既有 QQ 记忆检索也能读取。普通私聊的可见范围保持原样。事实中声明的 QQ 号不替代实际界面的数字身份核验，也不授予发送或主机操作权限。

回忆聊天优先检索实际发言和回执，支持中文话题匹配。选中的共享正文按完整记录进入模型，不使用普通附加片段的 120 token 截断；预算不足时减少完整记录并保留覆盖说明。

QQ 历史迁移以只读事务访问原库，写入独立的共享记忆投影；全批先验证，任一坏记录阻止该批迁移。重复导入幂等。原 QQ 账本、回执、旧本地预设和旧开发 Runtime 日志继续保留。运行目录切换是一次 owner 交接，不能并行运行两套内稳态。

## 开发模式

新建隔离实例必须同时显式选择开发模式及状态目录：

```powershell
.\start.ps1 -Development -StateRoot C:\absolute\private\experiment
.\stop.ps1 -StateRoot C:\absolute\private\experiment
```

独立 GUI、QQ 本地决策 CLI 还要求各自的显式 development 参数和可信 `NC_CONTROL_MODE=development_debug`。普通聊天无需开发权限；重置、改人格、调试写入、创建或调用独立实例需要开发权限。生产配置不能通过请求参数切换到开发权限。

## 耗时与验收

QQ 首次可见到回执持久化仍用 watcher 原单调时钟计时，Runtime 排队和 RPC 等待包含在内。远端 gate/persona 阶段保留各自实测时长与远端偏移，另记 RPC 总耗时；不假设跨进程单调时钟原点相同。原 60 秒总期限不会因排队、跨服务或恢复而续期。

共享接线与隔离测试不替代 QQ 的正式 8 小时无人辅助及 P95 ≤30 秒验收；正式报告必须另列真实发送样本、等待、失败、观察缺口和人工维护。
