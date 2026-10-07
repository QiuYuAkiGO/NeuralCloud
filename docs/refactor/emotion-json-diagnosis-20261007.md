# 情绪评价 JSON 解析拒绝诊断

本次诊断针对 `20261007-ui-snapshot-integrated-v3` 上线后首个 45 次情绪评价样本中的 3 次 `deepseek_structured_json_invalid`。已确认失败发生在 **Responses 文本提取后的 JSON 语法解析阶段**，尚未进入 JSON Schema 与情绪业务规则校验。原始响应未保留，受控回放未重现；具体错误字符和最初响应形态目前无法还原，不能把推断表述为已证实的供应商缺陷。

## 原始三次失败

以下时间均为北京时间，耗时为实际物理请求日志值。

| 请求开始时间 | HTTP | 耗时 | 输入 token | 输出 token | 结果 |
|---|---:|---:|---:|---:|---|
| 2026-10-07 23:20:36 | 200 | 2341.757 ms | 2527 | 107 | JSON 解析拒绝 |
| 2026-10-07 23:21:38 | 200 | 1069.323 ms | 2575 | 112 | JSON 解析拒绝 |
| 2026-10-07 23:24:16 | 200 | 894.308 ms | 2605 | 106 | JSON 解析拒绝 |

三次均请求 `https://api.deepseek.com/responses`，模型为 `deepseek-flash`。当前调用构造为 `text.format.type=json_schema`、`reasoning.effort=none`、`temperature=0`、`max_output_tokens=1536`，与隔离回放实际发出的请求一致。DeepSeek 的 [Responses API 文档](https://api-docs.deepseek.com/api/create-response/) 明确支持该 JSON Schema 格式；本次并没有发现切换成普通文本格式或缺少 `strict=true` 导致的证据，文档中的该格式未定义 `strict` 参数。

本地 `validate_response` 先要求响应为 `completed` 且没有顶层错误，再排除 refusal/tool response，最后执行 `json.loads`。只有走到这一步失败才会抛出本次错误码。因此：

- 已通过网络响应、响应完成状态和非工具输出检查。
- 输出只有 106–112 token，远低于 1536 上限；没有证据支持输出额度耗尽或请求超时。
- 缺少 `subject_refs`、枚举错误、情绪 source ID 不匹配等会在后续校验阶段失败，错误码不同。
- 历史 `finish_reason=null` 是日志字段尚未被填充：成功的 `ChatResult` 元数据在严格校验通过后才写入。它不能解释为供应商没有完成输出。

## 冻结输入回放

使用只读 SQLite 查询，逐一将原请求关联到 `ObserveEmotion` ledger 和 `emotion_reservations`。验证冻结 request/window 的哈希与当时 frame diagnostics 一致，再用现行且未更改的 `emotion-director-v5` 代码重建请求。每例均有 20 条窗口消息、1 条新增来源，实际提示输入分别为 5828、5953、5996 UTF-8 字节，低于 6000 字节上限。输入 token 数分别与线上原请求一致。

先每例回放一次，再循环五轮，合计 **18 次、每例 6 次，全部成功**。第二组设置最多 15 次调用，并在捕获相同 JSON 错误时提前终止。所有调用使用原有 DeepSeek 端点，在独立诊断进程运行；关闭业务请求日志与认知工作账本写入，不调用 `observe`、不回写情绪/记忆、不向 QQ 发消息。输出仅保留哈希、计数、协议形态和语法诊断。群接收策略在发送诊断模型请求之前检查。

18 次回放均通过 JSON 与 Schema 校验，无拒绝/工具调用；第二组额外记录的 15 次原始协议形态均为 `completed`、单个 message/单个 output_text。原始线上请求体和响应体没有留存，因而不能声称与失败响应逐字节相同，也不能将回放成功算作修复了历史三次评价。

截至 **23:40:03**，同一线上发布累计 **87 次完成、84 次成功、仍为原来的 3 次错误**；首个 45 次样本之后新增 42 次均成功。诊断回放未混入这组线上统计。

综合证据更符合偶发的生成内容或响应提取形态异常，不支持固定输入无效或持续配置故障的解释。由于失败时原始响应和解析位置均缺失，尚不能区分非法转义、括号/逗号错误、额外文本、空文本或多个 JSON 文本拼接。该判断属于推断，不能据此直接给 JSON 自动补括号、去解释文本或放宽校验。

## 观测缺口与实际影响

`harness/llm/deepseek_structured.py` 将 `JSONDecodeError`、其他 `ValueError`、`TypeError` 和 `RecursionError` 全部归为一个错误码，并丢弃异常链。合成的空输出、代码围栏、尾随逗号、两个对象拼接和非法数值五种样本均得到相同错误码及空 diagnostics。现有日志不能进一步定责；DeepSeek 文档也说明 Responses 服务不保存响应，无法通过接口取回这三份历史正文。

`EmotionService.observe` 捕获异常后使用空 `EmotionAppraisal`，保留既有情绪的时间衰减，将新增来源标记为 `processed=true`，并提交 `fallback` frame。三例数据库记录均符合该路径：

- `ObserveEmotion` ledger 为 `applied`，frame 为 `fallback`，新增 appraisal events 为零。
- 这表示这三条刺激的增量未获得评价，不代表其语义被成功解释。
- 相同 context 再访问会复用已提交结果，不会自动重新评价。
- 三例后续 `emotion_guidance` HTTP/格式校验通过，但分别在语义检查中得到 `emotion_guidance_feeling_missing`，使用兜底指引。这与 JSON 语法拒绝是两个不同校验阶段，不能将物理请求成功等同于最终语义成功。
- 三例的 `qq_gate` 均成功，其中一例有四次 `qq_persona` 成功请求。这里只证明后续决策继续，未据此声称所有消息均应当发言或已经发送。

## 建议的后续修复顺序

1. 在维持原有严格拒绝行为的前提下，补充不含正文的解析诊断：解析错误类别、行/列/偏移、文本长度、空输出/围栏标志、原始输出项和文本片段数量、响应状态、结束原因。将元数据在校验之前写入，避免失败路径永远缺失。不得记录异常的 `doc`、错误附近原文或供应商未筛选的报错文本。
2. 情绪评价可考虑在剩余原 deadline 内最多一次格式纠正或重试，保留总预算和准确调用成本；只有最终失败才消费本次来源并提交 fallback。不要后台重新兑现已经处理的三条旧刺激，避免重复计量和时序错位。该行为修改需要独立验证后上线。
3. 为语法失败、Schema 失败、语义兜底分别统计覆盖率；持续观察新错误位置分布，再决定是否修复具体协议映射或提示规则。

本轮只进行只读检查和隔离模型诊断，没有修改线上代码、重启服务或重置连续 shadow 窗口。诊断记录见 [脱敏证据](../harness/evidence/emotion-json-diagnosis-20261007/diagnosis.json)。

后续已按用户要求单独实现并部署无正文的错误反馈，见 [情绪错误反馈补充](emotion-json-feedback-20261007.md)。用户选择等自然流量再次出现错误后再分析；上述重试建议未实施，不继续反复回放历史模型请求。
