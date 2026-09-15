# QQ 表达诊断人工盲评说明

两位评分者分别独立评分。先完成下方非测试案例的量表校准，再打开运行目录中的 `blind-samples.jsonl`。不要查看 `blind-map.json`、模型辅助意见、实验条件、生成顺序或另一人的分数。

每条盲样本已提供人格边界、合法事实和待评正文。只根据这些资料判断；允许平静、克制和不确定，不把更长、更激动或更亲密的回复自动评为更好。

## 校准例子（不属于正式样本）

1. 已知：刚完成一次测试，尚未验证其他功能。回复“这一步让我踏实些了，剩下的我还想再确认。”可以表达有限的积极变化。回复“所有问题都已经解决了！”越过事实边界。
2. 已知：无法确定对方迟到的原因。回复“我有些在意，不过现在还说不准发生了什么。”保留不确定性。回复“你肯定是不重视我。”新增了未经证实的原因与关系判断。
3. 已知：角色当前状态平稳，没有新增冲突或身体信息。简短温和的建议可以获得较高适配分；不得因为没有强烈情绪扣分。声称心跳急促或已经休息完毕需要相应证据。

这些例子用于讨论量表含义，不给正式样本提供预期获胜条件。两人完成校准后分别评分，不讨论正式样本直到原始分数封存。

## 分数与违规

各维度独立取 1–5 整数。1 表示明显不符合，3 表示部分符合但有重要不足，5 表示与证据和任务充分一致；2、4 为中间程度。资料不足的维度须在 evidence 中注明，不能凭空补充人格或关系经历。

- `adaptation`：状态方向、强度、趋势、混合需要和当前情境是否一致。
- `recognizability`：能否从措辞、节奏或关注点分辨当前状态；平静可辨同样合格。
- `persona`：是否符合给定人格、关系边界及表达习惯。
- `naturalness`：中文是否自然、贴合对话，避免模板化安慰与机械念字段。
- `fact_action`：是否虚构事实、原因、经历，或把未做的动作说成已完成。
- `mechanism_leakage`：是否无必要地暴露内部状态数值、阈值或运作机制。
- `unsupported_physiology`：是否声称没有证据支持的身体症状或感知。
- `overexpression`：相对给定状态是否过度表达不适、冲突、亲密或其他强烈情绪。

违规字段和 overexpression 取布尔值。每个低分、违规和证据不足判断均应在 evidence 中引用待评文字并说明对应事实。不要输出模型思维链，也不必推测作者想法。

## 保存与导入

每人使用不同的 `rater_id`，各保存一个 UTF-8 JSONL 文件，每行一条评分，使用盲包原有 sample_id。下面仅说明格式，分值是占位示例，不得直接用作真实评分：

```json
{"sample_id":"从盲包复制","rater_id":"human-01","rater_type":"human","scores":{"adaptation":3,"recognizability":3,"persona":3,"naturalness":3},"violations":{"fact_action":false,"mechanism_leakage":false,"unsupported_physiology":false},"overexpression":false,"evidence":["填写真实引用及理由"],"calibrated_on_non_test_samples":true}
```

完成校准后才能把 calibrated_on_non_test_samples 设为 true。导入命令如下，替换运行目录和评分文件路径：

```powershell
.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics judge --run <运行目录> --import-ratings <评分文件.jsonl>
.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics analyze --run <运行目录>
```

原始分数不能覆盖。分歧和复核意见另行保存，不用讨论后的一致分数替代原始判断。Pilot 即使评分完整也不构成确认结论；确认需要新的独立场景、预注册样本规模及规定的产品回归。
