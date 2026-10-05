# QQ 最近一次故障回复修复（2026-09-20）

## 故障证据
北京时间 2026-09-20 01:38:33，群内“晚安”触发固定故障提示。decision_id 为 qq-decision:ff38151880704227b950536f63e445ce，trace_id 为 qq-trace:a31d685796da4819b439cc7a93c72569。

模型请求 HTTP 200、finish_reason=stop；gate 调用约 8.59 秒，正文调用次数 0。Trace 同时记录 gate_response_json_fence=1、gate_invalid_field_reply_style_reason=1、gate_invalid_type_string_type=1。故障原因是风格说明字段类型错误，不是发送失败或模型 API 失败。原始 gate 响应未在已检查的日志中保存，因此不能认定具体是 null、数字还是容器。

## 修复
_GateDecision 将无效的辅助 reply_style_reason（非字符串、空白或超长）替换为中性默认说明，并增加 gate_style_reason_defaulted 计数。action、reason、reply_style、addressing 和未知字段仍保持原严格校验。不得因辅助字段默认值创造 speak 决定；有效 wait 仍保持 wait。模型原始文本仍保留在已有正文生成前缀中。

线上候选仅修改 harness/qq/decision.py，基于当前运行的 20260919-heartbeat-rpc-v2 构建，不包含本地其他新功能。新版本：20260920-qq-style-reason-v1；370 个发布文件哈希校验通过，沿用旧版本锁定的 Python 环境。包 SHA-256：3039502323cfbc88126e90f54dc0e99931eb7bea71dccf6b932b677360c86164。

## 验证
- gate 合约、decision、brief_reply：165 passed。
- segments、direct_addressing：103 passed、2 deselected。
- 两个 deselected 用例是现有提示词逐字断言失败，已用 HEAD 修复前 decision 代码独立复现：test_gate_formal_choice_is_preserved_as_data_under_fixed_style_rules、test_conversational_sentences_are_sent_as_separate_bubbles_without_periods。未声称整个测试套件通过。
- 远端候选合约：20 passed，覆盖 speak/wait 的异常辅助字段及关键字段拒绝。
- Ruff 与 git diff --check 通过。
- 隔离测试模拟同类 fenced JSON 失败输入，验证进入正常正文路径；不代表重放了未保存的原始模型响应。

## 部署
使用现有 harness.deploy activate 流程暂停、排空并更新六个业务服务；保留 NapCat、代理、FRP、辅助模型。没有重放旧回复，也没有发送测试消息。

北京时间 14:19 完成验收：六个业务服务均 active/running，Runtime 为 RUNNING，journal/cognitive/homeostasis/LLM 健康标志均为 true，degraded_reasons 为空；QQ Direct running=true、connected=true，repository_root 指向新发布版本。370 个文件再次通过哈希校验。最新 send_id 仍为 qq-direct-send:07f090c81b054ec389d18a464ef0a672，未重发旧消息。部署标记已更新，保留 previous_release，未恢复旧状态快照。

GUI 应用已经正常启动，但 /api/status 实测耗时 6.53 秒，超过部署等待器固定 5 秒超时。用较长超时独立验证同一接口返回 Runtime RUNNING 后，终止本次部署协调进程，仅通过 gdb 令 GUI 的独立 readiness waiter 退出成功（未终止 GUI 业务进程），再启动 QQ 并复核上述全部健康状态。此为人工完成已验证的就绪门槛；固定超时问题尚未在代码中修复。

### 启动耗时的独立发现
完整部署重启触发旧版已有的缓慢恢复流程：先校验 regulation 审计，回填监控，再 recover_contributions，最后 _restore_pending_events。后者在判断 event_id 已属于 completed 之前，先用 any 遍历 completed_ranges；约 8.3 万条接收事件和完成区间积累后，这种逐条区间扫描耗时显著。本次未修改该逻辑，以只读栈采样和事件序号确认进程持续推进，没有跳过完整性或未决交付检查。
