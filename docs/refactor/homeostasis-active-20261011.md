# 内稳态 v3 active 验收与发布（2026-10-11）

用户确认 GUI 并非刻意关闭，授权恢复并继续验收。GUI 单独启动后健康接口可用，Runtime 保持 RUNNING。此次基于实际 `20261009-json-repair-v2` 做配置晋级，保留其 JSON 修复及其他已授权修改；不回退到旧诊断版本。

## 晋级前证据

真实快照于 UTC 10:15:33 通过 `shadow_report`：连续 137632.607137 秒，最后采样 UTC 10:15:30.985474，8762 条真实加工记录，817 条附带可选评价，7945 条无评价，工作量 82265867。累计最大间隔是历史统计，晋级依据为已重置后的连续时长及新鲜采样，不将远程 DNS 中断直接计为服务器离线。固定 224 条消息、其中 207 条只读群消息的隔离回放通过，不重放到线上。相关晋级与群关闭策略回归 22 项通过。

本地测试首次收集因缺少锁定依赖 json-repair 而失败，按 uv.lock 安装 0.63.5 后全部通过。此次未修改项目代码或依赖锁。

基础 manifest revision 5 在启动时叠加 Web、Server 配置修订，实际运行 revision 11；这是配置分层的正常结果。晋级必须保留这些配置，不以有效 revision 覆盖基础文件。

后台记忆累计 skipped 624，较此前 619 新增五个达到 8 次上限的 qq_model_validation_invalid_proposal 任务，分别于 UTC Oct8 15:42、16:04、16:23、16:43 与 Oct9 05:28 终止。保留审计，不清零、重排或宣称修复。晋级前队列空、数据库 WAL/quick_check 正常、锁错误 0。分阶段可选评价主要来自 qq_gate；后台记忆无评价不等同任务失败，goal_decision 等阶段的缺失仍作为校准限制保留。

## 部署保护

只通过 rollout promote 生成 profile 新 revision 和公平输入预算配置，manifest 同步绑定，注意力白名单只增加 communication.qq.attention.set.v1。该能力的 act 是模型选择种类；群授权入口由 attention_catalog 的 group.observe 检查控制，与 send 权限独立。

首次预检因快照响应包含额外 profile 元数据而停止，剔除响应元数据后用原始快照字段重跑门槛。停写后的第一轮备份发现其他研究任务的符号链接，原脚本拒绝继续；修正为保留链接并校验目标文本，不沿链接重复复制。两次保护停止均未提前应用 active 配置。

最终备份、配置和自然流量验收以 docs/harness/evidence/homeostasis-active-20261011/ 内 deployment.json、acceptance.json、natural.json 为准。任何历史失败任务均未重放；未主动发送群测试消息。

## 配置与备份结果

停写备份路径：`/var/backups/neuralcloud/20261011-before-homeostasis-active`。65,484 个普通文件逐项 hash 校验、7 个符号链接目标校验、2,902 个 SQLite quick_check 通过。归档 12,755,612,463 字节，SHA-256：`6cf735dd4ebc0afe5e2fbd1d0da1d58c444d9fba0c569f52f13439a56e9b9838`。归档后源文件 hash 不变。包含 WAL；恢复须停全部 writer 并匹配恢复完整文件集，不能覆盖运行中的数据库。

配置晋级为 `qq-social-drive-v1@4` active，基础 manifest 6，启用 Direct 公平输入预算。保留现有代码 release，未修改模型解析、调用重试、输出预算或模型选择。注意力数据库在停写备份与启动前 hash 相同。数据库未回滚，历史任务尝试数保留。

如需回滚数学规则，应基于当前配置生成更高 revision 的 mode=off，并同步 manifest；保持注意力数据库、群关闭策略和用户覆盖，不恢复旧状态快照，不回退到不认识关闭策略的代码。


## 上线健康通过，自然流量验收待完成

北京时间 18:35 的复查确认六服务 active、GUI 可用、Runtime RUNNING、QQ 已连接、无降级原因和 watchdog。独立被动推进器工作，profile revision 4 / active，基础 manifest 6、有效 manifest 12，公平输入预算已启用。Direct 经过模型默认值归一化后，除 attention_budget_enabled 外其他设置完全一致。三个群注意力策略与晋级前相同；本次快照均为 open/default/revision 0，并非声称当前存在关闭中的群。

Runtime 冷启动日志恢复约六分钟，期间未另行重启；启动初期反馈标志随后自然消失。后台记忆 applied 3801 / skipped 624，队列为空，真实工作 8762 项全部 applied。

自然观测自 UTC 10:33 起，截至 UTC 10:40 尚无模型请求样本，因此只确认配置上线和健康验收通过，**自然流量验收仍未完成**。不把空窗口视为零错误成功样本，也不通过主动群消息补样。v3 跟进已更新为 active 后的自然流量验收，保留当前 release 和群策略，不重复晋级或等待新的 shadow 24 小时。待取得真实自然工作证据并完成最终记录后，才停止跟进。


## 北京时间 19:11 自然流量跟进

纠正上一轮描述：18:42 导出的证据其实已包含 1 次后台记忆成功，上一轮“无模型请求”的状态标签与报告未随导出更新；18:40 的空窗口判断成立。本轮已纠正 natural.json 状态。自然流量已出现，但回复验收尚未通过。

本窗口后台记忆 7 次成功、guidance 3 次成功、情绪评价 2 次成功、gate 4 次成功、goal_decision 与 capability_selection 各 1 次成功、正文生成 2 次成功；正文校验 2 次错误。两次物理校验均精确关联同一 trace 和最终 error_fallback/model_invalid 决策，无发送回执，不能称为两个最终任务失败。

两次起点 UTC 10:45:21.504980 / 10:45:22.917050，HTTP200、completed/stop，输出 275 / 269 tokens，耗时 1400 / 1748 ms。顶层错误 deepseek_structured_json_invalid 诊断为空；读取服务器私有 repair 证据的安全字段确认细码 json_output_object_required。只在服务器分析输出结构，不导出正文：各有两个候选，现有逻辑选择最后一个，类型为 list，重复键数 0；原始严格 JSON 解析 Extra data 偏移 700 / 678。不是输出预算耗尽，也无证据说明 active 配置导致该问题。未调用模型重放、改解析策略、增加重试或重启。

六服务 active、Runtime RUNNING 无降级、QQ 连接、watchdog 空。真实工作 8785 全 applied（比上线初期 +23）、评价 821（+4），后台 applied3808 / skipped624，队列空。最近两个运行日志无新增 pause/emergency_stop 控制事件。保留 active，继续自然回复成功链路验收；同一已报告异常未变化不重复通知。证据 followup-1911.json。


## 北京时间 20:12 自然流量跟进

上线后累计记忆24、guidance23、appraisal14、gate21、goal9、capability4、正文生成11次成功；正文校验9成功3错误。第三次物理校验错误 UTC11:53:55 后，同一最终决策 ready，UTC11:54:02 获 qq_protocol_verified 回执，不能算最终失败。最终决策合计 ready8（其中3有验证回执）、natural_wait2、error_fallback3。后两次新增 error_fallback 也有验证回执，仍是兜底，不能当正常正文成功。

UTC11:37:30 的兜底在 decision.context_budget 阶段触发：最终输入62067字符+预留61541=123608，超过120000上限；是输入上下文限制，不是输出token预算。UTC12:02:01 的兜底发生在gate/persona调用均成功之后，尚需进一步核查业务校验原因，不把它归入JSON物理错误。无证据据此认定active数学配置故障，不回滚、不调整模型预算或重试。

六服务健康、Runtime RUNNING无降级、QQ连接、watchdog空；工作8886全部applied（上线初期+124）、评价838（+21），后台3824applied/skipped624、队列空，群策略保持相同。已取得正常自然回复及验证回执，但新增兜底原因仍待核查，跟进保留。证据followup-2012.json，不宣称零错误或永久修复。


## 北京时间 21:14 上线收尾

内稳态 active 配置部署、健康检查与自然流量验收完成，范围是该配置上线，不是修复所有模型/回复问题。实际 release 保持 json-repair-v2，profile@4 active、基础manifest6/有效12、输入预算和observe权限保持。六服务正常、GUI可用、Runtime RUNNING无降级、QQ连接，watchdog空。工作8977全部applied（较上线初期+215），评价845（+28）；后台3850applied/skipped624、队列空，最近两份运行日志无新增控制事件。

自然决策已增长为ready14、natural_wait3、error_fallback3；正常ready的协议验证回执数量见completion.json。持续观察新增成功后，最终兜底数没有增加，已满足真实工作及成功发送链路验收，不再以所有模型错误归零作为内稳态发布完成条件。

保留未修复限制：早期JSON候选选择导致的一次最终失败；一次输入上下文超限；UTC12:02:01的业务校验兜底。最后一例仅能由现有持久化确认物理调用成功而业务结果失败，未留存精确失败谓词，不能仅据reply_plan_invalid字段或通用model_invalid认定具体原因。未修改解析、放宽预算或重放来消除记录。另新增一次后台物理Schema maxItems拒绝，队列已空且skipped仍624，不作为新增最终任务失败。短窗口成功不证明这些问题永久解决。

保存completion.json，提交推送后停止v3上线跟进。备份及历史审计保留；后续如需修复上述模型/上下文问题应单独界定修复范围，不借上线验收扩大修改。
