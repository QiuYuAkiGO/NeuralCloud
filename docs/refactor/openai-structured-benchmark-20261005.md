# OpenAI Structured Outputs 接入调研与部分实测

**当前状态：按用户 2026-10-05 的决定暂缓 OpenAI 后续实现与评测，改用 DeepSeek 作为定稿接入。** 下文保留当时的调研、账户阻塞和部分数据；补测步骤仅作未来复现说明，不代表当前待执行任务。见 [DeepSeek 接入定稿](deepseek-structured-integration-20261005.md)。

2026 年 10 月 5 日，在 `codex/grounded-agency-rebuild-plan` 分支把 OpenAI Responses API 加入 CLM、DeepSeek 的冻结评测流程。**接口接通且已经取得真实输出，但完整对比被当前账户的余额和配额阻塞。现有数据不能支持用 OpenAI 替换 DeepSeek，也不能判断 OpenAI 的完整 64 条正确率。**

OpenAI 的 GPT-6 Luna 已尝试 44 条功能用例，31 条完整、符合 Schema 且与冻结标签一致，13 条没有可用输出。GPT-6.1 Sol 与 GPT-5.4 Mini 的单次预检均收到 `credit_balance_exhausted`，未开展批量测试。生产配置、业务代码和服务保持原状；本次只发送合成评测数据。

## 接口与模型选择

正式目标为 `https://api.openai.com/v1/responses`，使用流式响应、`text.format.type=json_schema`、`strict=true`、`store=false`。Schema 中所有属性均 required，禁止额外属性。Structured Outputs 约束输出结构，不能替代语义正确性检查；拒绝、截断和失败状态必须另外处理。[OpenAI 官方 Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)

首先选择 GPT-6 Luna 的 none 档作为速度候选、GPT-6.1 Sol 的 low 档作为质量候选。Luna 文档支持 none；Sol 不支持 none/minimal，low 是本次采用的最低推理档。启用推理时移除 temperature。账户 `/v1/models` 返回了这两个模型，但列表可见不代表余额、配额足以完成推理。[Luna 文档](https://developers.openai.com/api/docs/models/gpt-6-luna)、[Sol 文档](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[参数兼容说明](https://developers.openai.com/api/docs/guides/latest-model)

Luna 出现限流后，又预检了固定版本 `gpt-5.4-mini-2026-03-17`，作为可运行的候选备选；它支持 none 与 Structured Outputs，但该账户同样返回余额耗尽。没有把其他模型结果混入 Luna 统计。[GPT-5.4 Mini 文档](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

## 冻结输入与计量口径

复用原先的 [64 条中文用例](../../tools/production_acceptance/clm_cases_20261005.json)，SHA-256 为 `76eae06c9395582ed9bd3e355753e3811dfd47513a9cc5a1dd8cffbcd752f573`。请求的 state、question、候选、中文 instructions 与 DeepSeek 保持一致；没有加入正确标签，也没有根据本轮输出修改标签。提供给模型的 Schema 语义相同，仅增加 OpenAI 的严格模式与存储控制参数。

Luna none 使用 temperature 0、最大输出 128 tokens；Sol low 预检上限 1024 tokens。客户端使用复用 HTTP 连接，不自动重试、不修复输出。首个请求单列，不进入功能正确率。TTFT 从请求开始计到首个非空 output-text delta；完整延迟计到读取结束并完成本地校验，保存 terminal 事件时间供排查。

当前 Windows 机器直接 HTTPS 访问 OpenAI 发生 ConnectTimeout，继承已有环境代理后成功。所有 OpenAI 推理走这一代理；DeepSeek 前轮为直连，CLM 为各自机器的 loopback。**本次测量的是实际访问路径下的端到端延迟，不是纯模型推理速度。** 未从 Linux 服务器调用 OpenAI，不能把这里的数字视为 Linux 生产出口的实测值。

原用例是开发者制定标签的合成诊断集，未经独立人工审核。`gate-04` 的 wait/defer 边界仍有歧义；Luna 选择 wait 与冻结标签一致，DeepSeek 选择 defer，不能仅凭此认定前者业务上更正确。

## 已取得的结果

| OpenAI 项目 | 实测状态 |
|---|---|
| GPT-6 Luna，none | 44 次功能尝试；31 次可用且正确，13 次不可用；还有 20 条未测 |
| Luna 可用率 / 尝试中的正确结果比例 | 31/44，70.5%；包含接口失败，不能解释成纯模型语义准确率 |
| Luna 成功样本内正确数 | 31/31；有失败导致的样本选择偏差，不能外推为整体 100% |
| Luna 成功请求完整延迟 p50 / p95 | 2076.90 / 3447.76 ms，31 个成功样本，nearest-rank |
| Luna 首个输出文本 delta p50 | 1948.96 ms，含代理与公网链路 |
| GPT-6.1 Sol，low | 预检流内 `credit_balance_exhausted`，没有可用结果 |
| GPT-5.4 Mini 固定版本，none | 预检流内 `credit_balance_exhausted`，没有可用结果 |
| 匹配性能窗口、并发 2、正文 8 条、Schema 边界和长度探针 | 未运行；不填补数字 |

Luna 已测类别为：回复时机 12/12、证据关系 7/12、主体归属 8/8、工具选择 3/8、检索前 4 条中的 1/4。这些分子是“完整且正确”，分母含不可用请求；低分部分主要受失败影响，不能当作分类能力排名。布尔判断和等级评分尚未测到。

对于 Luna 实际尝试过的同一组 44 个 case_id，前轮原始结果中 CLM CPU 与 5090 都正确 21/44，DeepSeek none 为 43/44，DeepSeek low 为 42/44。Luna 是 31/44（含 13 次接口失败）。这个子集是因运行中断形成的前缀，并非预先抽样的独立测试集。

前轮完整评测仍以 [CLM 与 DeepSeek 实机报告](clm-deepseek-benchmark-20261005.md) 为准：两端 CLM 都为 32/64，DeepSeek none 为 62/64，low 为 61/64。前轮性能表使用固定 gate 状态的 16 次新状态窗口，与本次 Luna 的异构功能样本不同，不能将两张表的 p95 直接作为等条件速度排名。

## 阻塞与测量修正

Luna 的第一次运行在 3 个连续不可用请求后停止。一次独立诊断请求成功后，继续未记录的用例，原失败均保留；没有重跑失败用例来覆盖主结果。后续响应明确显示 `response.failed`、错误码 `rate_limit_exceeded`，尽管 HTTP 状态仍是 200。

13 次不可用请求中，8 次保存了明确限流错误码；较早的 5 次只保存了不可用终态与空输出，初版评测器未保存终态错误对象，不能事后断言它们的错误码相同。初始源码快照和恢复后的源码均保留。两个独立诊断请求均在主功能统计之外。

诊断请求还观察到响应头 requests limit 50、remaining 3、reset 约 22 小时 34 分钟。这里只记录当时账户响应，未据此推断所有 OpenAI 账户或模型的额度规则。Sol 与 Mini 的独立预检进一步确认 `credit_balance_exhausted`；已停止继续消耗请求。已请用户补足这份 OpenAI Key 对应的项目余额或提供可用配置。

评测器现在同时检查 HTTP 错误与流内终态错误：`rate_limit_exceeded`、`insufficient_quota`、`credit_balance_exhausted` 都会立即停止。已完成但拒绝的响应、截断后恰好可解析的 JSON、缺失必需字段、额外字段、未知枚举都不会计为可用成功。13 个测量单元测试通过，新增脚本 Ruff 检查通过。

## 补足额度后的执行与接入建议

保留本轮失败记录，另建输出目录开始新的完整测量窗口。主脚本一次完成 64 条功能用例、16 条新状态、30 条重复状态、16 条并发 2 和 8 条正文检查；补充脚本重放前轮 DeepSeek 的 7 个有效 Schema、1 个无效 Schema 和 4 个长度输入。补充脚本已实现但尚未进行真实 API 验证。

```powershell
& H:/NeuralCloud-evals/clm-20261005/venv/Scripts/python.exe tools/production_acceptance/openai_structured_benchmark.py `
  --key-file openai-api-key.txt `
  --cases tools/production_acceptance/clm_cases_20261005.json `
  --reply-cases tools/production_acceptance/deepseek_reply_cases_20261005.json `
  --output H:/NeuralCloud-evals/clm-20261005/results-openai-luna-none-funded `
  --model gpt-6-luna --effort none --network environment-proxy --performance
```

Sol 使用独立输出目录、`--model gpt-6.1-sol --effort low`。充值未必自动消除 Luna 的限额，需要预检返回的实际配额支持整个窗口；不足时明确记录未完成，不用切换别名绕过限制。

目前的接入判断是：OpenAI 的 JSON Schema 协议能够对接 NeuralCloud 的结构化决策接口，成功样本也显示了可用的中文判断能力；但**账户可用性、经代理的响应延迟、未完成的 64 条质量评测和正文质量**仍需验证。暂时保留前轮 DeepSeek none 优先进行业务接入验证的建议。无论采用哪家供应商，适配层都应区分结构校验、语义校验和传输可用性，并对限流、余额耗尽和截断提供可观测的失败状态。

## 证据与复现

- [汇总与哈希](../harness/evidence/openai-comparison-20261005/summary.json)：明确标记 `complete_comparison=false`，包含匹配子集统计。
- [Luna 部分报告](../harness/evidence/openai-comparison-20261005/luna-none-partial.json)：请求、预测、用量、失败、时间戳及恢复历史。
- [Sol 预检](../harness/evidence/openai-comparison-20261005/sol-low-preflight.json)、[Mini 预检](../harness/evidence/openai-comparison-20261005/mini-none-preflight.json)。
- [主评测器](../../tools/production_acceptance/openai_structured_benchmark.py)、[补充探针](../../tools/production_acceptance/openai_supplemental_benchmark.py)、[测量测试](../../tools/production_acceptance/test_openai_structured_benchmark.py)。

原始输出和运行时源码快照另保存在 `H:\NeuralCloud-evals\clm-20261005\results-openai-*`。证据打包时逐文件检查未包含完整 API Key；密钥未写入报告或版本控制。本轮未提交或推送。
