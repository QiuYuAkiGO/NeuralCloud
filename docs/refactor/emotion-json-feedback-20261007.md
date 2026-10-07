# 情绪评价 JSON 错误反馈补充

本次只补充可观测信息，便于下一次自然流量失败时定位原因。保留严格 JSON/Schema 校验、既有 fallback、原调用次数及 deadline；不自动修补输出、不新增重试、不重新处理历史三条刺激。

JSON 语法拒绝继续使用 `deepseek_structured_json_invalid`，新增 `error.diagnostics`：

- 固定枚举原因，包括缺值、缺属性名、缺冒号/逗号、字符串未结束、非法转义、非法 Unicode 转义、非法控制字符、额外文本、BOM 和嵌套过深。
- `offset` 为从零起算的 Unicode 字符位置，`line`/`column` 从一起算；仅解析器提供位置时记录。
- 文本字符数、是否空白、是否以 Markdown 代码围栏开头。

物理请求日志在校验前记录响应完成状态、输出项/消息/文本片段数量、结束原因、文本类型/长度及原有 token 统计；失败后也可区分空输出、多个片段和已完成响应。情绪评价与演绎指引的持久化 diagnostics 新增 `structured_error`，保存具体代码及同一组安全诊断，原 `fallback_reason` 保持兼容。已提交兜底重新加载后可读到这些细节。

不记录提示词、响应正文、错误位置附近原文、`JSONDecodeError.doc`、未筛选的异常文本或密钥。语法原因通过固定映射生成，不直接保存解析器错误字符串。重复键、非有限数值和 Schema 错误保留原有独立分类。

247 项相关回归通过，覆盖普通与诊断 HTTP 路径、真实 parser 行列位置、单次调用限制、私密标记不进入运行日志、多文本片段、深层 JSON、情绪兜底持久化与重载、deadline、用量记录、群聊记忆和情绪调用链。Ruff 通过。

已部署 **`20261007-emotion-json-diagnostics-v1`**，代码 `a21d0e9`，发布包 SHA-256 `bf75c1d41115139b098a45b10823747a594090db6257b6c9a42cb9c744a9168d`。以 `20261007-ui-demand-v1` 为基线，核验仅三个上述生产文件变化，保留 UI 按需刷新及已有语义校验修复。停写备份 `/var/backups/neuralcloud/20261007-before-emotion-json-diagnostics` 校验通过，群策略及 profile/manifest/Direct 配置保留。六个服务 active、Runtime RUNNING、QQ connected。

服务器候选发布包使用模拟响应验证了错误位置、私密标记不进入日志、兜底记录持久化与重载，未调用真实模型或向群发消息。上线后自然流量已出现新的响应形态和结束原因字段；不为验证而制造线上错误。详细阶段计数保留在证据中，历史错误不计入本次发布。

内稳态继续 shadow，profile `qq-social-drive-v1@3`、manifest revision 4。维护后的持续观察起点按服务器记录推算为北京时间 **2026-10-08 00:05:12**，无新断档时最早 **2026-10-09 00:05:12** 满 24 小时。

[部署与自然流量证据](../harness/evidence/emotion-json-feedback-20261007/deployment.json)。
