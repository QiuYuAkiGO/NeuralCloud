# QQ 正文单换行分段（2026-09-17）

模型有时在两句口语之间只放一个换行、且没有句号。发送校验不允许单条气泡含换行，于是失败关闭并改发错误兜底。

## 规则

- 正文生成出口把剩余的单个换行（含 CRLF）当作段边界，拆成独立气泡后再校验。
- 空段或超过三条仍失败关闭。
- 不改写措辞，不按词汇命中拦截。
- JSON 决策对象仍在任何拆分之前失败关闭。
- 回执校验器不变：已发出的单条气泡仍不得含换行。

## 实现

- [harness/qq/decision.py](../../harness/qq/decision.py)：`_prepared_group_segments` 在段落拆分之后、口语句号拆分之前做 `line_segmentation`。
- 行为约定见 [QQ 简短回复](../QQ_BRIEF_REPLIES.md)。

## 验证

相关回归 133 passed；Ruff 通过。覆盖生产现场的两行中文无句号样本、CRLF、正式回复保留句号、四行或空段仍失败关闭，以及 JSON 拦截仍发生在拆分之前。

## 发布

- 版本：`20260917-line-break-v1`，上一版 `20260917-json-body-guard-v1`。
- 发布包 SHA-256：`88f30dee40914ef0174e4a44a9d65d048f2066d01c071d765b9ef975fea72599`；369 个文件。
- 六个业务服务均为 active/running。QQ Direct `connected=true`、`automatic_send=true`、`state=watching`；图片模式仍为 `off`。
- 群 `1044946257` observe/send 为 true；群 `1060979317` 的 observe/send 仍为 false。
- 激活后 journal 无新的 err 级记录。Direct `last_error_type=direct_incoming_invalid` 为现场既有字段，本轮未当作本次分段改动引入的故障。
- 本轮未发送测试消息。回退使用 `20260917-json-body-guard-v1`。

证据见 [evidence/qq-line-break-2026-09-17.json](evidence/qq-line-break-2026-09-17.json)。
