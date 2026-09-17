# QQ 回复言之有物（2026-09-17）

减少点评式和敷衍回应，同时保持 20–60 字、1–2 句的简短目标。

## 规则

每条回复至少承载一项实质内容：具体观点或判断及一个理由、与话题相关的具体信息或自身偏好经验、针对具体细节的有针对性疑问、或直接问题的答案本身。禁止整段只有评价、感叹或附和，禁止无承接的敷衍反应。赞同或反对须落到对方话中的具体细节。言之有物不靠增加篇幅实现。

开口策略沿用较克制门槛，并按渠道分流：

- ambient 闲聊若只能给出点评而无实质增量，优先等待。
- direct 有新需求时仍须作答，并落到具体内容。
- 纯重复点名仍优先等待。

人格 `speech_style` 增加对应条目，版本 `misty-digitalbody-0.2.1`。GUI 主对话与 QQ 正文均消费该人格。

## 实现

- [harness/qq/decision.py](../../harness/qq/decision.py)：群 gate `qq-group-turn-taking-v23-substance`，私聊 `qq-private-turn-taking-v9`；规划、自主发言与私聊入口同步实质约束。
- [harness/qq/draft.py](../../harness/qq/draft.py)：正文生成规则；私聊由群规则替换生成。
- [presets/builtin/misty.json](../../presets/builtin/misty.json) 与工作副本 `data/presets/misty.json`：`speech_style` 与版本。
- 行为约定见 [QQ 简短回复](../QQ_BRIEF_REPLIES.md)。

不新增词汇命中式发送拦截；风格偏差仍只记录。不增加模型调用或后置改写。

## 验证

相关 QQ 与人格回归 235 passed；Ruff 通过。契约测试断言克制参与前缀保留，且实质条款进入群/私聊 gate 与正文两段提示词。

## 发布

- 版本：`20260917-substance-v1`，基于当时生产 `20260917-board-diary-repair-v3`。
- 发布包 SHA-256：`59d091286f8d6a0d5e3cfa037bb25646475cf22cdb18f026fcabab4b80499a05`；369 个文件校验通过。
- 激活后六个业务服务均为 active/running。QQ Direct `connected=true`、`automatic_send=true`、`state=watching`；图片模式仍为 `off`。群 `1060979317` 的 observe/send 仍为 false。
- 工作副本与内置人格均为 `misty-digitalbody-0.2.1`，`speech_style` 含言之有物条目。
- 旧提示词与发布包封存于 `/var/backups/neuralcloud/substance-variants-20260917/`。未恢复旧业务快照。
- 激活后 journal 无新的 err 级记录。Direct `last_error_type=direct_incoming_invalid` 为现场既有字段，本轮未当作本次提示词改动引入的故障。自然群聊质量仍待观察，本轮未发送测试消息。

证据见 [evidence/qq-substance-2026-09-17.json](evidence/qq-substance-2026-09-17.json)。
