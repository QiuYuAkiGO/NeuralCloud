# Misty 巴别塔长轮对话

本协议通过现有 Runtime 向 Misty 连续讲述巴别塔故事，记录每轮交付、记忆提交、模型调用链路和内稳态。成功完成一次实验时，共有 **100 条用户输入 + 100 条 Misty 逻辑回复**；一条回复中的多个交付片段合并计数，历史消息、system 消息和工具事件不计入这 200 条。

本页说明当前实现与操作方法。数据校验、假服务或假时钟测试不构成真实实验结果；现场状态以指定输出目录的 `run.json`、逐轮证据和报告为准。

## 1. 固定故事与对话安排

完整内容在 [babel_story.json](babel_story.json)，格式为 `babel-story-v1`。前 90 轮为九章故事，每章 10 段，正文每段 140–200 个非空白 Unicode 字符；第 91–100 轮为固定的十条感想问题。引言只在第一轮正文前注入，说明故事根据《创世记》11:1–9 扩写，阿珥、米拉、沙兰及其生活细节属于虚构。

输入顺序预先冻结。故事阶段允许 Misty 自然简短回应，不插入记忆测验、情绪暗示或内稳态询问；即使 Misty 提前评论或提问，后续输入仍按数据集发送。第 90 段明确结束故事，第 91 轮才开始集中询问感想。第 91 轮是第一次完整故事后的整体感想，第 92–100 轮已经受到此前问答影响，应作为连续对话分析。

每轮必须确认回复交付成功、对应 `CommitInteraction` 成功以及 Runtime 处理完成，随后默认等待 **10 秒**再发下一条。间隔使用客户端单调时钟，实际间隔保存在逐轮记录中；它不计入本轮回复耗时。运行过程中沿用现有会话、记忆和上下文裁剪策略，完整故事保存在实验输入中，但不保证每轮模型上下文同时包含此前全部原文。

## 2. 校验、启动与运行

以下 PowerShell 命令从仓库根目录执行，使用已经安装项目依赖的虚拟环境。先做不调用模型的内容校验：

```powershell
.\.venv\Scripts\python.exe -m eval.basic_runtime.babel validate
```

默认故事无需额外参数；`--story <文件>` 可指定同一协议格式的数据集，十条感想问题仍须与固定版本一致。

**现有四进程服务全部离线时**，使用保存的配置恢复服务，并在恢复后开始实验：

```powershell
.\.venv\Scripts\python.exe -m eval.basic_runtime.babel run --restore --out-dir .tmp/babel/live-01
```

`--restore` 当前只支持 Windows 启动器和默认本地 Runtime 地址 `http://127.0.0.1:8770`。它要求现有 `data/`、`runtime/active-start.json` 与 `gui_settings.json` 可读，已保存的模式为真实 `live` 或 `ollama`，预设为 `misty`。恢复前会复制整个状态目录到新的 `.tmp/babel-backups/<UTC 时间戳>/`，另存 manifest/profile 配置副本、校验值、启动日志与回执，然后按保存的配置路径启动原状态目录。

这里的“恢复”指恢复现有服务与存档，不是重置角色或恢复到实验前状态。对话会继续写入当前存档，实验结束后也不会自动回滚。若端口仍被占用或存在待核实的进程登记文件，恢复会停止；先核对运行中的进程身份，不要靠删除登记文件绕过检查。

启动器要求 Runtime 为 RUNNING；若现有存档恢复后仍为 PAUSED，运行器会记录暂停现场、零发送并结束，不自动恢复。启动失败留下的本次进程登记由项目停机脚本核对身份后清理。身体服务须事先可用；Windows Docker 引擎正常不代表 Ubuntu-24.04 内的 Docker WSL 集成已经启用。

**服务已经运行时**，使用下面这一种方式直接连接，省略 `--restore`；此路径不执行离线备份：

```powershell
.\.venv\Scripts\python.exe -m eval.basic_runtime.babel run --out-dir .tmp/babel/live-01
```

输出目录必须全新，且与现有状态目录互不包含。`.tmp/babel/live-01` 是本地私有实验目录示例；后续尝试使用新的名称，已有目录不会被覆盖。运行前检查当前会话确为 Misty、模型可用、完整调用观测已启用、服务健康且没有待处理事件或动作。检查失败不会切换预设、清空记忆或插入故事。

默认状态目录为仓库下的 `data/`，控制令牌从 **`data/control.token`** 读取；可用 `--state-root <目录>` 和 `--control-token-file <文件>` 明确指定。未找到默认令牌文件时可读取 `NC_CONTROL_TOKEN`。不要把令牌正文放到命令行或实验文档中。

每轮等待上限默认 300 秒，可用 `--turn-timeout-seconds` 设置；`--interval-seconds` 可修改默认 10 秒间隔。非默认参数会写入运行记录，比较实验时需要一起核对。

## 3. 停止与迟到结果

Runtime 暂停、watchdog 保护、服务故障、非回复终态、配置漂移、超时或关键证据缺失都会停止后续发送。模型、persona、profile、会话或 Runtime 实例在实验中发生变化，会记为 `CONFIGURATION_DRIFT`。监控流可以重连，分页采集以持久序号推进；连续监控不可用超过 10 秒会中止。

HTTP 接收回执丢失时，运行器按本轮关联 ID 从 journal 事件恢复接收证据，**不会重发输入**。超时或中断并不撤销 Runtime 已经接受的消息；停止发送后默认再观察 30 秒，保存迟到结果和最终快照，不自动暂停、恢复或重置服务。

`run.json.status` 保留本次停止的事实。停止后到达的结果作为补充证据保存，不倒算成按协议完成的回合。当前没有实验断点续跑命令：再次 `run` 是从第一轮开始的新实验；`--restore` 也不会跳过已发送的故事。

只有 100 轮均完成、实际输入状态可精确关联，且原始 journal 和模型调用证据齐全时，整次运行才标记 `COMPLETED`。对话完成但归档缺失会标记 `INCOMPLETE_EVIDENCE`。进程成功退出码为 0，运行未完整完成时为 2；应同时读取 `stop_reason` 和已完成轮数。

## 4. 耗时、链路与内稳态口径

逐轮记录通过 `correlation_id`、`event_id`、`receive_seq`、`command_id` 和 `prepare_id` 关联接收、排队、认知准备、交付和记忆提交。认知阶段及模型请求记录进一步提供 span 父子关系、主/辅助模型角色、调用阶段、耗时、重试和可取得的 usage；后台调用归属原始关联 ID，不因完成较晚而并入下一轮。

耗时按来源区分：

- `accept_observed_ms`：发送至客户端观察到接收回执或恢复出的接收证据。
- `delivery_observed_ms`、`commit_observed_ms`、`completed_observed_ms`：发送至客户端观察到交付、提交和处理完成；含传输及监控采集延迟，采用同一客户端单调时钟。
- `queue_wall_derived_ms` 与域命令 `wall_derived_ms`：依据服务端 UTC 时间戳推算的排队和命令耗时；不与跨进程单调时钟混算。模型请求的 `latency_ms` 单独保留。

认知阶段、模型调用与后台任务可能重叠，不能将它们简单相加当作端到端耗时。当前没有 token 流式观测，**TTFT 不可用**，`ttft_ms` 为 `null`；首个阶段通知、完整回复交付都不能替代首 token 时间。报告分别给出故事/感想阶段的平均值、P50、P95、最大值和较慢回合，分位数采用线性插值。

每轮保留发送前 `before_send` 和提交后 `after_commit` 的完整内稳态快照，但二者不冒充模型实际输入。`analysis.used_for_response` 将 `PrepareResponse` 路由记录中的 revision、summary hash，与命令版本向量、实际 `homeostatic_interoception` 内容及同版本完整状态连接；全部匹配后才记 `state_exact=true`，无法匹配则明确记证据不完整。

完整状态包含七个维度 `energy_reserve`、`cognitive_load`、`safety_predictability`、`competence`、`autonomy`、`social_connection`、`novelty`，以及维度后验/目标区间/证据、`core_affect`、派生 `emotion`、候选策略、watchdog、revision、profile 与内感摘要。七维和派生情绪属于规则驱动的功能性代理量，文本长度、词语和心跳也可能直接改变状态；状态变化与 Misty 的感想自述分别记录，不将前后快照差异全部归因于某一句故事。

## 5. 私有证据与可分享副本

一次运行在输出目录保留：

- `story.json`、`run.json`：实际输入副本、内容哈希、代码/配置指纹、参数、运行状态和归档结果。
- `turns/001.json` 至 `turns/100.json`、`turns.jsonl`、`turns.csv`：已尝试轮次的输入、回复、阶段、时间、关联链路和状态。未尝试的轮次不会伪造文件。
- `initial_snapshot.json`、可取得的 `final_snapshot.json`、`monitor.jsonl`、`index.sqlite3`：前后快照、持续采集事件和本地索引。
- `background.jsonl`：后台心跳、身体观察、主动表达等额外事件；不计入 100 轮逻辑问答。
- `raw/journal.jsonl`：选定连续区间的原始完整 journal 行及必要前置锚点，校验序号、checksum、hash chain 和文件摘要。经过脱敏的 monitor 投影不能替代此字节级证据。
- `model_calls.jsonl`、`calls/`、`call_files.json`：关联的模型请求记录、详细调用文件及文件校验信息。
- `transcript.md`、`summary.json`、`report.md`：可读对话、阶段统计和带局限说明的报告。

这个目录及离线备份含现有会话、存档和详细调用内容，按本地私有证据保存。`shareable/` 是运行结束时生成的**凭据脱敏副本**：处理敏感字段和本次已知密钥，并移除 embedding；它仍可保留对话、状态与调试正文，不等于完整匿名化，也不包含可用于验证原始 journal 字节的替代品。分享前按接收范围检查内容。

## 6. 重新生成报告与验证边界

对已有输出目录重新生成报告：

```powershell
.\.venv\Scripts\python.exe -m eval.basic_runtime.babel report --out-dir .tmp/babel/live-01
```

`report` 不发送消息、不启动 Runtime；若原调用日志路径仍可读，它会收集迟到的模型调用文件，记录 `late_evidence_refresh` 并重写汇总报告。它不重新判定已停止的实验为完成，也不会自动刷新现有 `shareable/`；查看或分享时应核对副本对应的生成阶段。

实现入口为 [babel.py](babel.py)，证据与统计逻辑为 [babel_evidence.py](babel_evidence.py)。离线遥测和跨模块链路用例见 [test_babel_telemetry.py](../../tests/test_babel_telemetry.py) 与 [test_babel_trace_integration.py](../../tests/test_babel_trace_integration.py)。实际命令、环境和结果统一记入 [BR-1 执行记录](../../docs/milestones/BASIC_RUNTIME_V1_EXECUTION.md)。校验通过、fixture 完成 100 轮或遥测回归通过，均不等于 Misty 已完成真实 100 轮；本实验也不能替代 BR-1 的真实 8 小时或 M5 的 24–72 小时验收。
