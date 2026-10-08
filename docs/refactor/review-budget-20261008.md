# 回复审查输出预算及新增字段兼容（2026-10-08）

用户要求适度提高输出 token 预算，并忽略新增字段。本次针对发生截断的回复语义审查路径，将 max_tokens 从 3072 调整为 4096（增加约 33%）；原 8 秒内部 deadline 和单次调用保持。更多 token 不保证可以在 deadline 内完成，需同时观察长度耗尽和时间耗尽。

SemanticReview 与 ClaimReview 使用 extra=ignore，生成的传输 Schema 允许额外字段，Pydantic 将未知字段丢弃，不将其写入结果或用于判定。必填字段、枚举、长度、证据引用、逐句覆盖、重复键和完整 JSON 检查保留。此修改不扩展到能力执行、配置或后台记忆 Schema。JSON 文档末尾 extra_data 属语法问题，不是对象新增字段，本次不裁剪或接受它。

96 项结构化客户端、语义校验、上下文及审计测试通过，Ruff 通过。新增测试覆盖未知顶层/claim 字段通过传输但不进入结果、未知字段不能覆盖拒绝结论、无效枚举/超长理由/越界证据仍拒绝，以及 4096 预算、单次调用和 8 秒超时。服务器隔离模拟验证通过，没有模型重放。

代码提交 06a02d6。部署基于实际 20261008-semantic-output-v1，仅修改 semantic.py 两处 extra 策略和 validator.py 的 token 预算，保留服务器其他语义规则与已有修复。配置、群策略和用户覆盖保持原样。内稳态仍 shadow，维护停机后连续观察重新计算。

## 上线验证

北京时间23:34:15完成20261008-review-budget-v1部署。六服务active、Runtime RUNNING、QQ连接。启动初期qq_homeostatic_feedback降级标志在23:35:32复查中已消失，未执行额外重启。首批5次自然后台记忆调用成功，无新模型错误；暂未出现新版回复审查样本，不宣称线上截断率已经下降。

停写备份30,719文件逐项校验、1,157数据库quick_check通过，归档4,658,996,524字节，SHA-256 c6854bf00c95c543b20d004f04278129e81667284f9bc62922d974b6a26adc45，路径/var/backups/neuralcloud/20261008-before-review-budget。配置和注意力策略保持不变。

manifest5/profile qq-social-drive-v1@3 shadow。连续45.822315秒、last_sample UTC15:34:52.677340，起点约北京时间23:34:06.855；最早次日同刻连续24小时，仍须以持久化计数重算。rollout shadow_report仍因时长不足不通过。
