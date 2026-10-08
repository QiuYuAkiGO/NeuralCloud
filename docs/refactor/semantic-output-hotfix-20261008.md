# 回复语义审查输出约束热修复（2026-10-08）

712342021 群北京时间 22:40 的两次正文生成均成功，后续语义审查分别因 claims[0].reason 超过 400 字符、JSON 对象重复键失败。前一决策 error_fallback 被更新上下文取消发送；后一决策 error_fallback 于 22:40:51 取得 qq_protocol_verified 回执。22:41 后续正文与审查成功，但也因更新上下文取消发送。审查失败不能归因于 QQ 发送、后台 subject_refs 或闲置模型清理。

修复在语义审查系统指令中明确单 JSON 对象、每个对象字段唯一、独立断言用数组元素表达。reason 定义为短分类理由，建议单句 120 字符以内，保留 400 字符硬上限并在字段 Schema description 提示。要求保留逐句覆盖，不能以省略断言换取简短输出。

重复键拒绝现在提供 unique_keys 诊断，仅显示宿主 Schema 已定义的字段名，未知字段统一为 *，不记录值、正文或模型推理。仍拒绝完整不合规结果，不截断理由、不去重接受 JSON，不增加模型调用或重试，不修改 deadline、证据规则或兜底发送策略。该修改减少输出歧义，不能保证模型永不再违反约束，需要自然流量验证。

91 项结构化客户端、语义校验、校验上下文及审计测试通过；包含 400/401 字符边界、嵌套重复键、未知键脱敏、单次物理调用断言。Ruff 与 diff 检查通过。服务器在实际版本上模拟验证有效响应、超长拒绝、已知/未知重复键诊断通过，无模型请求、无历史刺激重放。

生产包基于 20261008-subject-constraints-v1，仅更改 harness/validator/semantic.py 和 harness/llm/deepseek_structured.py；保留服务器已存在且本地分支尚未包含的其他语义规则。代码提交 46b656d。首次维护健康请求 5 秒超时，发生在 Runtime 停止前；操作脚本将本次部署健康等待下限延长至 20 秒后重试，不修改服务代码或配置。部署结果见同目录证据。

## 部署及自然流量

北京时间 23:16:59 部署完成，六服务 active、Runtime RUNNING、无降级、QQ 连接。23:18:46 自然调用样本包括 4 次正文生成与 4 次语义审查全部成功，无新物理错误；该群 4 个新决策全部 ready，其中 2 个取得发送回执。不能将少量样本解释为永久消除模型格式违规。

停写备份 `/var/backups/neuralcloud/20261008-before-semantic-output`：30,191 文件哈希验证、1,157 数据库 quick_check 通过，归档 4,615,310,039 字节，SHA-256 `7ef9194e569aaa40d9bc53d48ad40e71c009f3636ea9ee2ccb82ee5480810e78`。全部源哈希不变，配置及注意力策略保留。

内稳态仍为 qq-social-drive-v1@3 shadow、manifest revision 5。真实快照 rollout shadow_report 仅因未满 24 小时不通过：continuous_seconds=74.078751，last_sample_at=2026-10-08T15:18:05.399733Z，连续起点约北京时间 23:16:51.321。最早次日同刻满 24 小时，后续仍须以持久化计数重算；本次热修复不等于 active 上线完成。

## 23:21 跟进发现

上线初期四次成功之后，23:20:17/27 新增两次情绪评价 extra_data，分别 285 字符/offset284、276 字符/offset275，HTTP200/completed/stop。持久化均 ledger applied、frame fallback、0 appraisal events、来源 processed；guidance 为 model，后续正文及审查成功，两决策 ready，其中后一条有发送回执。没有原始尾字符证据，不推测额外字符是什么。

23:21:00 回复审查另一次 HTTP200/incomplete/length，输出恰为 3072 token，与当前上限一致。决策 error_fallback/model_invalid，核查时没有发送回执。该事件属于输出长度耗尽，不是重复键或 reason 长度限制复发。该窗口语义审查13成功1错误；不能继续引用初期零错误作为当前结果。

六服务健康、Runtime RUNNING、QQ连接、无新控制pause，WAL quick_check正常、锁错误0。内稳态连续267.368868秒，样本15:21:18.689850Z，尚未24小时。后台skipped仍619；出现未到上限的重试任务，不能当最终失败。未重启、重放、重排或放宽校验。脱敏证据见followup-2321.json。
