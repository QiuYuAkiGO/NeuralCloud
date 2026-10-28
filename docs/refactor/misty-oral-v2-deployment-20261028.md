# Misty 口语表达 v2 部署

日期：2026-10-28（北京时间）。用户明确要求上线并推送代码。

新增一个口语表达提示段：闲聊接一个具体点、简单回应及时收尾、减少逐字解释玩笑和无依据的情绪判断；保留具体观点、贴切吐槽及文学表达。明确请求仍按需完整回答。六组虚构示例不作为记忆或事实。

部署以实际运行的 `resident-20261028-context-budget-v1` 为基线，发布为
`resident-20261028-oral-v2`。身份、事实边界、权限、工具和发送协议保持原样；没有修改接话调度、模型参数、记忆或上下文压缩。

完整 Harness 测试 **59 通过、0 失败**。新增集成检查覆盖真实提示组装、重复组装不叠加新段，以及原有人格、图片理解、记忆和发送边界仍然存在。部署门禁 `GATES_PASSED`，`external_actions=false`。

上线后服务 `active/running`，运行目录核验为新发布；状态 `READY`，输入待处理数为 0，官方渠道 ready，OneBot ready 且 connected。会话绑定保持一致，最近部署窗口无 error 级服务日志。没有主动发送 QQ 测试消息，因此不声称新风格已获线上实际回复或人工验证。

本次基于探索性偏好评审上线，并未达到原完整实验的正式胜出标准。评分原文、聊天记录、解盲键与本地页面不提交 GitHub。后续每轮人工评审最多 30 条。

- [可复现部署代码及回滚步骤](../../tools/deploy/misty_oral_v2/README.md)
- [脱敏部署回执](../harness/evidence/oral-expression-v2-20261028/deployment.json)

备份：`/var/lib/misty/backups/oral-v2-20261028`。服务使用 `60-oral-v2.conf`；回滚移除该覆盖即可恢复原 `50-context-budget.conf`，不要覆盖上线后的状态数据。
