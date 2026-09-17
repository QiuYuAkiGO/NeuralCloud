# QQ 正文拦截 gate JSON（2026-09-17）

正文阶段为复用 DeepSeek 前缀缓存，会续接 gate 的「只输出 JSON」消息。模型有时再次写出决策对象，而不是口语。validator 关闭，既有检查只拦空正文、超长和换行，单行 JSON 会发出去。

## 规则

- 发送前识别「整段正文是 `action` 为 speak/wait 的 JSON 对象」（含完整 JSON 代码围栏）。
- 失败关闭：改用既有错误兜底，不把 JSON 改写成口语，不抽取 `first_point`。
- 检查只放在正文生成出口 `_prepared_group_segments`；`_validate_reply_segments` 仍接受历史回执里已存的泄漏文本。
- 提示词要求正文只输出自然语言，不复述决策字段。不新增词汇命中式风格拦截。

## 实现

- [harness/qq/decision.py](../../harness/qq/decision.py)：结构检测与发送前拒绝。
- [harness/qq/draft.py](../../harness/qq/draft.py)：群/私聊正文规则。
- 行为约定见 [QQ 简短回复](../QQ_BRIEF_REPLIES.md)。

## 验证

相关回归 285 passed；Ruff 通过。覆盖结构检测、真实正文生成路径失败关闭、句号拆分前拦截，以及历史 JSON 回执文本仍可校验。

## 发布

- 版本：`20260917-json-body-guard-v1`，上一版 `20260917-substance-v1`。
- 发布包 SHA-256：`119d02c6ea73b80e67cfe1591494428946c989aba4b601425bd9ed9264d216b7`；369 个文件。
- 六个业务服务均为 active/running。QQ Direct `connected=true`、`automatic_send=true`、`state=watching`；图片模式仍为 `off`。
- 群 `1044946257` observe/send 为 true；群 `1060979317` 的 observe/send 仍为 false。
- 本轮未发送测试消息。回退使用 `20260917-substance-v1`。

证据见 [evidence/qq-gate-json-body-guard-2026-09-17.json](evidence/qq-gate-json-body-guard-2026-09-17.json)。
