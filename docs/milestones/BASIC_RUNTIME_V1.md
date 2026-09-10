# BR-1 · 基本功能可用里程碑

> 持续更新；建立于 2026-09-09，Asia/Shanghai。总进度入口：[PROJECT_PROGRESS](../PROJECT_PROGRESS.md)。
> 用户确认的验收条件：**真实连续 8 小时**；Linux 操作必须经过 **聊天 → MistyAI 桌面终端 → 命令 → 实际输出核验**。
> 当前状态：**实现与回归进行中，未达到发布验收**。加速时钟、模拟桌面、模型连通均不能替代真实环境出口条件。

## 1. 目标与完成判据

BR-1 是现有 M0～M3 基础能力的交付切片，不重编号、不替代原 M0～M5 或 GA-01～08 的研究结论。五项全部通过才关闭里程碑；任何一项 FAIL/BLOCKED/INCONCLUSIVE 均保持开放。

| 门禁 | 关联模块 | 必须达到的结果 | 证据 |
| --- | --- | --- | --- |
| BR-ONLINE | NC-09/17/18/25 | 正常启动四进程；真实 8 小时不崩溃、不重启、不进入 PAUSED/FAULTED；健康、心跳、事件记录持续可读。 | 完整 28,800 秒单调时钟、30 秒采样、至少 961 条样本、四个 PID 及创建身份、服务日志。 |
| BR-HOMEO | NC-11/12/14 | 无用户/控制干扰时七维状态有限、处于硬边界内、后验归一化；任何维度不连续 300 秒停留在硬边界附近；正常恢复持续发生。 | 同一次 8 小时曲线/样本；加速动力学回归作为补充。真实 physiology 启用时必须保留耗能与耗尽保护。 |
| BR-TERMINAL | NC-10/13/15 | 用户用自然语言请求，角色通过 Linux 桌面终端完成测试任务；完成报告由本任务实际可访问性文本与退出码支持。 | 动作意图、批准、独立 verify、A11y 内容哈希、BEGIN/EXIT 标记、最终聊天交付。禁止用主机 shell 输出代替桌面证据。 |
| BR-CHAT | NC-05/07/09/15 | 20 轮真实模型对话均有非空交付，无重复回复；正常命令在配置时限内完成；慢记忆提交不阻塞健康接口。 | 每轮请求、回复、prepare/commit 耗时、交付凭证；超时/恢复的确定性测试。 |
| BR-PERSONA | NC-02/06/16 | 20 轮中身份、年龄、生日等固定锚点无矛盾；最近约定能承接；中间重启后恢复同一角色和会话。 | 默认小雾：19 岁、5 月 20 日；真实回复人工核对及上下文/持久化回归。不能只凭名字命中判定整体人设一致。 |

首版范围：文本对话；单行、可打印 ASCII 命令，每任务一条命令，可包含 shell 的正常组合语法。交互式密码、sudo、全屏编辑器、长期 shell 会话、多个并行终端任务暂不属于 BR-1。终端输入/Enter 沿用现有动作权限，需要在 `/ops` 批准；步数上限 12、任务时限 600 秒、批准等待上限 180 秒。重启会停止未完成的终端计划，已提交动作仍按既有 reconcile 处理，避免自动重复命令。

## 2. 从项目进度表得到的实际缺口与执行结果

| 工作项 | 原缺口 | 本轮执行 | 剩余工作 / 当前判断 |
| --- | --- | --- | --- |
| P1 内稳态恢复 | 无变化事件和仅更新能量的事件重置全局更新时间，导致其他维度恢复时间被反复吞掉。 | 改为按各维度更新时间积分；保留追赶上限、真实能耗与 watchdog；增加 480 次心跳、扰动、中途重启及高频中性事件回归。 | 加速回归通过；真实 8 小时未运行。 |
| P2 最近上下文 | Session 从旧到新拼接后截断前缀，预算紧张时最新约定丢失。 | 最近完整会话优先于检索记忆；保留最新后缀；来源引用只指向实际保留内容。 | 默认小雾 persona 本身未被截断；真实长对话一致性仍需 20 轮验收。 |
| P3 日志资源 | journal 启动全量读入；在线查询与新命令幂等查找反复扫描整个历史。 | 512 条正文尾部缓存、128 条紧急缓冲、稀疏定位与分页；命令索引只保存序号等元数据。历史哈希链和磁盘记录保留。 | 元数据索引、认知命令账本及终态动作仍随历史增长；不能据本轮改动宣称恒定内存。由真实长跑资源测量决定后续治理。 |
| P4 聊天到终端 | 默认聊天不能形成受约束 GUI 多步任务；窗口名称不能证明命令输出。 | 增加显式启用的规划器与 Runtime 桥；每步重新观察；批准前后复查目标；BEGIN/EXIT 限定本任务输出；失败/未知停止；新消息中断；重复批准不重执行。 | 模拟桌面完整链路通过；真实 Linux 联调被 Docker 环境阻塞。 |
| P5 MistyAI 输出 | AT-SPI 只导出节点名称，无终端 Text 内容；可选充电扩展缺文件导致默认服务导入失败。 | 外仓增加有界 Text 读取与保护节点排除；校验真实 A11y vendor MIME/哈希；无站点时不加载缺失扩展，显式启用时明确失败。 | 外仓默认 API/grounder 回归通过；尚未部署。充电站没有实现，不能把模拟时间推进或禁用耗能当作充电。 |
| P6 真实模型时限 | 首次现场测试发现记忆 commit 可耗 64.59 秒并阻塞事件循环，30 秒调用超时无法及时返回。 | 修复命令线程、取消与持久化边界；为新预设明确配置 120 秒命令时限，HTTP 与 Runtime 对齐。 | 修复和真实复验结果见下方证据记录；首次失败保留。 |
| P7 可重复验收 | 无符合这次 8 小时定义的、不会把 smoke 算通过的采样器。 | 增加只读 soak CLI，校验真实时长、采样缺口、部署身份、七维状态、心跳与外部干扰；独立 JSONL/原子 summary。启动器新增 `-StateRoot` 隔离存档及令牌文件。 | 环境恢复后先跑短 smoke，再运行完整 8 小时。 |

不阻塞 BR-1 的后续建设：TXT 角色导入、关系 belief/care、训练数据导出、完整工作/恢复自主决策、GA 长期因果/社会研究。NC-20 的历史业务失败继续保留；完成本里程碑不能覆盖该失败。

## 3. 复现配置与启动

固定配置：[basic-runtime-v1](../../presets/runtime/basic-runtime-v1.json)、[functional-proxy-v1@3](../../presets/homeostasis/functional-proxy-v1.json)。默认小雾，表达 `full`，身体 `grounded`，真实终端必需能力开启；模型标注为本次现场配置 `deepseek-v4-flash`。改变模型时另存并更新 manifest revision，连同实际 GUI 配置（排除密钥）记录；manifest 标签本身不会切换 API 模型。

从仓库根目录在 PowerShell 执行。先用 [GETTING_STARTED](../GETTING_STARTED.md) 恢复 Docker Desktop/WSL/MistyAI，部署包含本轮 AT-SPI Text 修复的外仓版本，再运行：

```powershell
.\dev.ps1 doctor -Mode live -NonInteractive -NoOpen -Json
$acceptanceState = Join-Path $PWD ('.test-tmp/basic-acceptance-' + (Get-Date -Format yyyyMMdd-HHmmss))
.\start.ps1 -Mode live -ManifestPath presets/runtime/basic-runtime-v1.json -StateRoot $acceptanceState
```

`-StateRoot` 将 Runtime、Homeostasis、Cognitive、角色工作副本、日志放到新目录；首次从本机 GUI 设置复制模型配置。该目录包含本地凭据，**不是可分享的验收报告**。提交/分享时只复制排除凭据后的日志摘要和测试证据。不要删除原 `data`；原启动流程与 `stop.ps1` 继续通过根目录进程登记管理单套服务。

先完成聊天/终端测试，再停止外部交互并等待至少两个正常心跳；此后启动 soak：

```powershell
$registry = Get-Content -Raw .neuralcloud.pids.json | ConvertFrom-Json
$soakArguments = @(
    '-m', 'eval.basic_runtime.soak',
    '--control-token-file', $registry.control_token_file,
    '--service-token-file', $registry.service_token_file,
    '--out-dir', 'eval/reports/basic_runtime'
)
foreach ($service in $registry.services) {
    $soakArguments += @('--pid', "$($service.name)=$($service.pid)")
}
# 短测试只检查采样链路；正常结束也应 INCONCLUSIVE、退出码 1。
& .\.venv\Scripts\python.exe @soakArguments --duration-seconds 120
# 正式运行：默认 28,800 秒；期间不聊天、不批准/暂停/重启、不修改配置。
& .\.venv\Scripts\python.exe @soakArguments
```

正式运行需电脑保持唤醒、网络可用。采样器不会注入心跳、操控桌面、修改内稳态或自动重启服务；外部用户/控制事件、服务故障、漏采、异常时钟或长期触边会导致 FAIL。短测试、测试时钟或缺失四进程身份不能得到 PASS。`summary.json` 的 `full_milestone_verdict=NOT_EVALUATED` 是有意保留的范围边界：soak 只检查前两项门禁。

停止本次四进程仍使用 `./stop.ps1`；保留独立目录用于调查。新一轮正式 8 小时使用新报告目录，不能拼接多次短跑、重启或暂停后的时间。

## 4. 验证记录与证据

验收案例：[BASIC_RUNTIME_V1_TEST_CASES](BASIC_RUNTIME_V1_TEST_CASES.md)。真实模型首次现场记录：[basic-live-chat-2026-09-09.json](evidence/basic-live-chat-2026-09-09.json)。最终回归结果与环境快照持续补充在 [执行记录](BASIC_RUNTIME_V1_EXECUTION.md)。

本轮新增的确定性证据覆盖：

- [内稳态 8 小时加速](../../tests/test_basic_homeostasis_stability.py)、[人设及会话重启](../../tests/test_basic_persona_continuity.py)。
- [journal 有界历史](../../tests/test_runtime_journal_bounded.py)、[命令索引](../../tests/test_runtime_command_index.py)。
- [终端规划与证据](../../tests/test_basic_terminal_actions.py)、[Runtime 完整链路](../../tests/test_basic_terminal_runtime.py)、[批准等待与目标变化](../../tests/test_basic_terminal_approval.py)。
- [soak 采样器](../../tests/test_basic_runtime_soak.py)、[隔离启动](../../tests/test_start_script.py)。

## 5. 当前剩余出口

1. 完成真实模型慢提交修复后的回归与现场复验。
2. 恢复 Docker Desktop，部署 MistyAI 改动，完成真实桌面终端案例。
3. 完成 20 轮对话/人设人工核对与资源基线测量。
4. 在同一配置下完成无干扰真实 8 小时采样，复核五项门禁后再标记 BR-1 通过。

每次执行后更新本页缺口表、执行记录、测试案例状态和总进度表。未经实际证据支持，不把“待验收”改为“通过”。
